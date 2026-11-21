import os
import re
import sys
import time
import base64
import secrets
import threading
import traceback
import requests
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, Response

app = Flask(__name__)

# --- VARIABLES DE ENTORNO (BASE ORIGINAL + ACCESO AL PANEL) ---
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "bot_melamina_cejas_2026")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "").strip()

app.secret_key = os.environ.get("FLASK_SECRET_KEY") or f"{PANEL_PASSWORD}_creas_y_aprendes_secret_2026"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

TZ_PERU = timezone(timedelta(hours=-5))
MIME_SOPORTADOS_GEMINI = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}
MAX_BYTES_GEMINI = 5 * 1024 * 1024  # 5 MB máximo para proteger RAM en Render
URL_VIDEO_GANCHO = "https://cdn.jsdelivr.net/gh/abelsantosmamani18-crypto/bot-whatsapp@main/static/velas_y_jabones/video-gancho.mp4"

# --- CONFIGURACIÓN DE REMARKETING INTELIGENTE (2 HORAS PAREJAS) ---
REMARKETING_MINUTOS = float(os.environ.get("REMARKETING_MINUTOS", "120"))
REMARKETING_HORA_INICIO = (8, 30)   # 8:30 a.m. hora Lima
REMARKETING_HORA_FIN = (22, 0)      # 10:00 p.m. hora Lima
VENTANA_META_SEGUNDOS = 23.5 * 3600 # Margen seguro dentro de las 24h gratis de Meta

# --- MEMORIA RAM Y BUZÓN TEMPORAL (BASE ORIGINAL) ---
historial_conversaciones = {}
buzon_mensajes = {}
buzon_media = {}
timers = {}
candados_clientes = {}
mensajes_procesados = set()
ESPERA_SEGUNDOS = 5.0

# --- MEMORIA DE REMARKETING (100% RAM) ---
timers_remarketing = {}
estado_remarketing = {}

# --- MEMORIA DEL PANEL EN VIVO (100% RAM, SIN GOOGLE SHEETS) ---
clientes_panel = {}
mensajes_panel = {}

# --- EL CANDADO DE SEGURIDAD ---
semaforo = threading.Lock()


def ahora_peru():
    return datetime.now(TZ_PERU).strftime("%d/%m/%Y %H:%M")


def log(mensaje):
    """Imprime directo a stderr con flush inmediato para visibilidad real en Render."""
    print(f"[{datetime.now(TZ_PERU).strftime('%H:%M:%S')}] {mensaje}", file=sys.stderr, flush=True)


def limpiar_etiquetas_para_panel(texto):
    if not texto:
        return ""
    def _reemplazar(match):
        tipo = match.group(1).upper()
        archivo = match.group(2).strip().split("/")[-1]
        return f"[{tipo}: {archivo}]"
    return re.sub(r'\[(VIDEO|IMAGE|DOCUMENT|AUDIO):([^\]]+)\]', _reemplazar, texto, flags=re.IGNORECASE).strip()


def esta_bot_pausado(telefono):
    with semaforo:
        cliente = clientes_panel.get(telefono)
        return bool(cliente and cliente.get("estado_bot") == "PAUSADO")


def _cancelar_timer_remarketing_locked(telefono):
    """Debe llamarse únicamente cuando `semaforo` ya está adquirido."""
    t = timers_remarketing.pop(telefono, None)
    if t:
        t.cancel()


def cancelar_timer_remarketing(telefono):
    with semaforo:
        _cancelar_timer_remarketing_locked(telefono)


def _obtener_estado_rmk_locked(telefono):
    """Debe llamarse únicamente cuando `semaforo` ya está adquirido."""
    if telefono not in estado_remarketing:
        estado_remarketing[telefono] = {
            "precio_enviado": False,
            "link_enviado": False,
            "pago_confirmado": False,
            "enviado": False,
            "ultima_interaccion_cliente": time.time(),
        }
    return estado_remarketing[telefono]


def registrar_actividad_cliente_rmk(telefono, texto_cliente=""):
    """Cancela cualquier remarketing pendiente y registra si el cliente indica que ya pagó."""
    texto_low = (texto_cliente or "").lower()
    senales_pago_cliente = (
        "ya pagu",
        "ya lo pagu",
        "ya hice el pago",
        "ya cancel",
        "ya lo cancel",
        "ya yape",
        "ya lo yape",
        "ya deposit",
        "ya transfer",
        "ya compr",
        "ya me inscrib",
        "ya me lleg",
        "acceder a mi producto",
        "mis compras",
        "no me llega el correo",
    )
    with semaforo:
        _cancelar_timer_remarketing_locked(telefono)
        est = _obtener_estado_rmk_locked(telefono)
        est["ultima_interaccion_cliente"] = time.time()
        if any(s in texto_low for s in senales_pago_cliente):
            est["pago_confirmado"] = True


def actualizar_y_programar_remarketing(telefono, respuesta_carla):
    """Actualiza la etapa comercial según la respuesta de Carla y programa el toque único de 2 horas."""
    resp_low = (respuesta_carla or "").lower()

    senales_precio = ("s/ 110", "110.00", "bonos.pdf", "go.hotmart.com")
    senales_link = ("go.hotmart.com",)
    senales_post_pago = (
        "activados automáticamente",
        "activan automáticamente",
        "quedan activados",
        "quedaron activados",
        "ya tienes separado tu cupo",
        "acceder a mi producto",
        "bonus - comunidad de apoyo",
        "grupos de apoyo",
        "cambio del correo electrónico",
        "qué alegría que ya estés adentro",
        "celebremos",
    )

    segundos_espera = None

    with semaforo:
        est = _obtener_estado_rmk_locked(telefono)

        if any(s in resp_low for s in senales_precio):
            est["precio_enviado"] = True
        if any(s in resp_low for s in senales_link):
            est["link_enviado"] = True
        if any(s in resp_low for s in senales_post_pago):
            est["pago_confirmado"] = True

        cli = clientes_panel.get(telefono)
        pausado = bool(cli and cli.get("estado_bot") == "PAUSADO")

        # Si ya pagó, ya se envió remarketing antes, está pausado o aún no vio el precio -> no programar
        if pausado or est["pago_confirmado"] or est["enviado"] or not (est["precio_enviado"] or est["link_enviado"]):
            _cancelar_timer_remarketing_locked(telefono)
            return

        # Calcular hora exacta respetando horario humano de Lima (8:30 a.m. a 10:00 p.m.)
        ahora = datetime.now(TZ_PERU)
        objetivo = ahora + timedelta(minutes=REMARKETING_MINUTOS)

        inicio_hoy = objetivo.replace(
            hour=REMARKETING_HORA_INICIO[0],
            minute=REMARKETING_HORA_INICIO[1],
            second=0,
            microsecond=0,
        )
        fin_hoy = objetivo.replace(
            hour=REMARKETING_HORA_FIN[0],
            minute=REMARKETING_HORA_FIN[1],
            second=0,
            microsecond=0,
        )

        if objetivo < inicio_hoy:
            objetivo = inicio_hoy
        elif objetivo >= fin_hoy:
            objetivo = (objetivo + timedelta(days=1)).replace(
                hour=REMARKETING_HORA_INICIO[0],
                minute=REMARKETING_HORA_INICIO[1],
                second=0,
                microsecond=0,
            )

        # Verificar que el envío programado siga dentro de la ventana gratis de 24h de Meta
        ultimo_ts = est.get("ultima_interaccion_cliente", time.time())
        if (objetivo.timestamp() - ultimo_ts) > VENTANA_META_SEGUNDOS:
            _cancelar_timer_remarketing_locked(telefono)
            log(f"[REMARKETING OMITIDO] Caería fuera de la ventana de 24h de Meta para {telefono}")
            return

        segundos_espera = max(1.0, (objetivo - ahora).total_seconds())
        _cancelar_timer_remarketing_locked(telefono)

        nuevo_timer = threading.Timer(segundos_espera, ejecutar_remarketing, args=[telefono])
        nuevo_timer.daemon = True
        timers_remarketing[telefono] = nuevo_timer
        nuevo_timer.start()

        modo = "POST_LINK" if est["link_enviado"] else "POST_PRECIO"
        log(f"[REMARKETING PROGRAMADO] {telefono} ({modo}) para {objetivo.strftime('%d/%m %H:%M')} ({int(segundos_espera)}s)")


def ejecutar_remarketing(numero_remitente):
    """Genera con Gemini un seguimiento humano contextual y lo guarda en el historial para continuar el flujo."""
    global historial_conversaciones

    with semaforo:
        timers_remarketing.pop(numero_remitente, None)
        if numero_remitente not in candados_clientes:
            candados_clientes[numero_remitente] = threading.Lock()
        candado_cliente = candados_clientes[numero_remitente]

    if not candado_cliente.acquire(timeout=45):
        log(f"[REMARKETING ERROR] Timeout esperando candado para {numero_remitente}")
        return

    try:
        with semaforo:
            cli = clientes_panel.get(numero_remitente)
            if cli and cli.get("estado_bot") == "PAUSADO":
                return
            if numero_remitente in buzon_mensajes and buzon_mensajes[numero_remitente]:
                return

            est = estado_remarketing.get(numero_remitente)
            if not est or est.get("pago_confirmado") or est.get("enviado"):
                return
            if not (est.get("precio_enviado") or est.get("link_enviado")):
                return

            ultimo_ts_al_iniciar = est.get("ultima_interaccion_cliente", 0)
            if (time.time() - ultimo_ts_al_iniciar) > VENTANA_META_SEGUNDOS:
                log(f"[REMARKETING CANCELADO] Ventana 24h vencida para {numero_remitente}")
                return

            modo = "POST_LINK" if est.get("link_enviado") else "POST_PRECIO"
            memoria = list(historial_conversaciones.get(numero_remitente, []))

        if not GEMINI_API_KEY:
            log("[REMARKETING ERROR] GEMINI_API_KEY no configurada.")
            return

        try:
            with open("prompt_ventas.txt", "r", encoding="utf-8") as archivo:
                instruccion_base = archivo.read().strip()
        except Exception as e:
            log(f"[REMARKETING PROMPT ERROR] {e}")
            instruccion_base = "Eres Carla, asesora de ventas de Creas y Aprendes."

        texto_historial = "\n\nÚltimos mensajes de contexto:\n" + "\n".join(memoria) if memoria else ""

        if modo == "POST_PRECIO":
            orden_interna = (
                "\n\n[ORDEN INTERNA DE SEGUIMIENTO — NO LA MENCIONES AL CLIENTE: "
                "Han pasado 2 horas desde tu último mensaje y el prospecto no ha respondido. "
                "Ya vio el precio promocional de S/ 110.00, pero todavía no pidió el enlace oficial. "
                "Redacta UN solo mensaje de seguimiento por WhatsApp (SOLO TEXTO, sin etiquetas [VIDEO]/[DOCUMENT]/[IMAGE] y SIN pegar el enlace todavía) "
                "sonando 100% humana, cercana, natural y breve (máximo 3 párrafos cortos). "
                "Ten en cuenta lo último que conversaron para no repetir frases mecánicas, y apóyate con naturalidad en estos dos alivios reales: "
                "1) Que no necesita comprar máquinas caras porque empieza con utensilios que ya tiene en casa, y vendiendo sus primeros *4 sets* ya podría recuperar los *S/ 110* que invirtió en el curso. "
                "2) Que no compra a ciegas: *Hotmart* la respalda con *7 días de garantía* para entrar, revisar las clases por dentro y si ve que no es para ella, le devuelven el *100% de su dinero*. "
                "Deja el último párrafo SOLO para una pregunta breve y empática invitándola a contarte con confianza si hay alguna duda puntual que todavía la frena.]"
            )
        else:
            orden_interna = (
                "\n\n[ORDEN INTERNA DE SEGUIMIENTO — NO LA MENCIONES AL CLIENTE: "
                "Han pasado 2 horas desde tu último mensaje y el prospecto no ha respondido. "
                "Ya recibió el enlace oficial de Hotmart, pero todavía no ha confirmado su pago. "
                "Redacta UN solo mensaje de seguimiento por WhatsApp (SOLO TEXTO, sin etiquetas [VIDEO]/[DOCUMENT]/[IMAGE] y SIN volver a pegar la URL del enlace) "
                "sonando 100% humana, cercana y breve (máximo 3 párrafos cortos). "
                "Retoma el hilo natural según lo último que hablaron (por ejemplo el método de pago que mencionó): "
                "pregúntale con naturalidad si pudo abrir el enlace o si le apareció algún detalle en pantalla, "
                "recuérdale brevemente que su pago entra directo por *Hotmart* con total seguridad y cuenta con *7 días de garantía* para revisar el programa por dentro con tranquilidad, "
                "y cierra en el último párrafo SOLO ofreciéndole ayuda inmediata si se trabó en algún paso (como con *Yape* o el método que eligió).]"
            )

        prompt_rmk = f"{instruccion_base}{texto_historial}{orden_interna}"
        url_gemini = f"https://generativelanguage.googleapis.com/v1/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
        payload_gemini = {
            "contents": [{"parts": [{"text": prompt_rmk}]}],
            "generationConfig": {"temperature": 0.25},
        }

        res = None
        for intento in range(2):
            try:
                res = requests.post(
                    url_gemini,
                    headers={"Content-Type": "application/json"},
                    json=payload_gemini,
                    timeout=(10, 30),
                )
                if res.status_code == 200:
                    break
                log(f"[REMARKETING GEMINI ERROR] Intento {intento + 1} HTTP {res.status_code}: {res.text[:200]}")
                if res.status_code in (429, 500, 502, 503, 504) and intento == 0:
                    time.sleep(2.0)
                    continue
                break
            except requests.exceptions.RequestException as e:
                log(f"[REMARKETING GEMINI EXCEPCION] Intento {intento + 1}: {e}")
                if intento == 0:
                    time.sleep(2.0)
                    continue

        if res is None or res.status_code != 200:
            return

        data = res.json()
        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content", {}).get("parts") or []) if candidates else []
        texto_rmk = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()

        # Limpieza defensiva: el remarketing de 2 horas es siempre solo texto
        texto_rmk = re.sub(r'\[(VIDEO|IMAGE|DOCUMENT|AUDIO):[^\]]+\]', '', texto_rmk, flags=re.IGNORECASE).strip()
        if not texto_rmk:
            return

        # Revalidación final antes de enviar (por si el cliente escribió o pausaste mientras Gemini pensaba)
        with semaforo:
            cli = clientes_panel.get(numero_remitente)
            if cli and cli.get("estado_bot") == "PAUSADO":
                return
            if numero_remitente in buzon_mensajes and buzon_mensajes[numero_remitente]:
                return
            est = estado_remarketing.get(numero_remitente)
            if not est or est.get("pago_confirmado") or est.get("enviado"):
                return
            if est.get("ultima_interaccion_cliente", 0) != ultimo_ts_al_iniciar:
                return

        exito = enviar_mensaje_whatsapp(numero_remitente, texto_rmk)
        if exito:
            with semaforo:
                est = _obtener_estado_rmk_locked(numero_remitente)
                est["enviado"] = True
                memoria_actual = list(historial_conversaciones.get(numero_remitente, []))
                memoria_actual.append(f"Tú: {texto_rmk}")
                historial_conversaciones[numero_remitente] = memoria_actual[-12:]

            registrar_interaccion(
                telefono=numero_remitente,
                remitente="CARLA",
                texto=texto_rmk,
                tipo="text",
            )
            log(f"[REMARKETING ENVIADO] {numero_remitente} ({modo})")
    except Exception:
        log(f"[REMARKETING EXCEPCION] {traceback.format_exc()}")
    finally:
        candado_cliente.release()


def registrar_interaccion(telefono, remitente, texto, tipo="text", media_id="", nombre="", forzar_etapa=None):
    """Guarda instantáneamente en la memoria RAM del panel (sin red ni hilos extra)."""
    fecha = ahora_peru()
    orden = time.time()
    remitente_norm = remitente.upper()
    resumen = (texto or "").replace("\n", " ").strip()
    if not resumen and tipo == "image":
        resumen = "[Imagen]"
    elif not resumen and tipo == "document":
        resumen = "[Documento]"
    resumen = resumen[:120]

    with semaforo:
        cli = clientes_panel.get(telefono)
        if not cli:
            cli = {
                "telefono": telefono,
                "nombre": nombre or telefono,
                "estado_bot": "ACTIVO",
                "etapa": "Nuevo",
                "ultimo_mensaje": resumen,
                "ultima_fecha": fecha,
                "notas": "",
                "no_leidos": 0,
                "_orden": orden,
            }
            clientes_panel[telefono] = cli
        else:
            if nombre and nombre != telefono:
                cli["nombre"] = nombre
            cli["ultimo_mensaje"] = resumen
            cli["ultima_fecha"] = fecha
            cli["_orden"] = orden

        if forzar_etapa and cli["etapa"] in ("Nuevo", "Interesado"):
            cli["etapa"] = forzar_etapa

        if remitente_norm == "CLIENTE":
            cli["no_leidos"] = cli.get("no_leidos", 0) + 1

        if telefono not in mensajes_panel:
            mensajes_panel[telefono] = []

        mensajes_panel[telefono].append({
            "remitente": remitente_norm,
            "texto": texto,
            "fecha": fecha,
            "media_id": media_id,
            "media_type": tipo if media_id else "text",
        })
        if len(mensajes_panel[telefono]) > 150:
            mensajes_panel[telefono] = mensajes_panel[telefono][-150:]


# --- FUNCIONES DE WHATSAPP CLOUD API ---
def descargar_media_whatsapp(media_id):
    """Descarga bytes y MIME type desde Meta Cloud API usando el media_id."""
    if not WHATSAPP_TOKEN or not media_id:
        return None, None
    try:
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        meta_res = requests.get(
            f"https://graph.facebook.com/v21.0/{media_id}",
            headers=headers,
            timeout=(10, 15),
        )
        meta_res.raise_for_status()
        meta_data = meta_res.json()
        media_url = meta_data.get("url")
        mime_type = meta_data.get("mime_type", "application/octet-stream")
        if not media_url:
            return None, None

        file_res = requests.get(media_url, headers=headers, timeout=(10, 20))
        file_res.raise_for_status()
        return file_res.content, mime_type
    except Exception as e:
        log(f"[META MEDIA ERROR] ({media_id}): {e}")
        return None, None


def enviar_mensaje_whatsapp(telefono_destino, texto_mensaje):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log("[META ERROR] Falta WHATSAPP_TOKEN o PHONE_NUMBER_ID.")
        return False

    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono_destino,
        "type": "text",
        "text": {"body": texto_mensaje},
    }

    try:
        respuesta = requests.post(url, headers=headers, json=payload, timeout=(10, 15))
        if respuesta.status_code >= 400:
            log(f"[META TEXTO ERROR] HTTP {respuesta.status_code}: {respuesta.text[:250]}")
            return False
        return True
    except requests.exceptions.RequestException as e:
        log(f"[META TEXTO EXCEPCION] {e}")
        return False


def enviar_multimedia_whatsapp(telefono_destino, tipo_media, url_archivo, nombre_archivo="", caption=""):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log("[META ERROR] Falta WHATSAPP_TOKEN o PHONE_NUMBER_ID.")
        return False

    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    tipo_lower = tipo_media.lower()

    payload = {
        "messaging_product": "whatsapp",
        "to": telefono_destino,
        "type": tipo_lower,
        tipo_lower: {"link": url_archivo},
    }

    if tipo_lower == "document" and nombre_archivo:
        payload[tipo_lower]["filename"] = nombre_archivo

    if caption and tipo_lower in ["video", "image", "document"]:
        payload[tipo_lower]["caption"] = caption

    try:
        respuesta = requests.post(url, headers=headers, json=payload, timeout=(10, 20))
        if respuesta.status_code >= 400:
            log(f"[META MEDIA ERROR] HTTP {respuesta.status_code}: {respuesta.text[:250]}")
            return False
        return True
    except requests.exceptions.RequestException as e:
        log(f"[META MEDIA EXCEPCION] {e}")
        return False


# --- CEREBRO DE CARLA (GEMINI 3.5 FLASH-LITE + VISIÓN DE IMÁGENES Y PDF) ---
def procesar_mensaje_gemini(numero_remitente, texto_recibido, media_recibida=None):
    global historial_conversaciones

    if esta_bot_pausado(numero_remitente):
        return

    respuesta_final = "Lo siento, tengo un problema técnico temporal."
    gemini_ok = False

    if GEMINI_API_KEY:
        try:
            url_gemini = f"https://generativelanguage.googleapis.com/v1/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
            headers_gemini = {"Content-Type": "application/json"}

            try:
                with open("prompt_ventas.txt", "r", encoding="utf-8") as archivo:
                    instruccion_base = archivo.read().strip()
            except Exception as e:
                log(f"[PROMPT ERROR] No se pudo leer prompt_ventas.txt: {e}")
                instruccion_base = "Eres un asistente de ventas."

            with semaforo:
                memoria = list(historial_conversaciones.get(numero_remitente, []))

            texto_historial = ""
            if memoria:
                texto_historial = "\n\nÚltimos mensajes de contexto:\n" + "\n".join(memoria)

            nota_vision = ""
            partes_adjuntas = []

            if media_recibida:
                for item in media_recibida[:2]:
                    contenido_bytes, mime_real = descargar_media_whatsapp(item.get("media_id"))
                    mime_usar = (mime_real or item.get("mime_type") or "").split(";")[0].strip().lower()
                    if contenido_bytes and len(contenido_bytes) <= MAX_BYTES_GEMINI and mime_usar in MIME_SOPORTADOS_GEMINI:
                        b64 = base64.b64encode(contenido_bytes).decode("utf-8")
                        partes_adjuntas.append({
                            "inline_data": {
                                "mime_type": mime_usar,
                                "data": b64,
                            }
                        })

                if partes_adjuntas:
                    nota_vision = (
                        "\n\n[INSTRUCCIÓN DE VISIÓN: El prospecto adjuntó una imagen o documento PDF. "
                        "Obsérvalo con atención (puede ser un comprobante de Yape/Tarjeta, un voucher CIP pendiente o pagado de PagoEfectivo, "
                        "una captura del correo de Hotmart o un error en pantalla) y responde exactamente según lo que ves y tus reglas del prompt.]"
                    )

            prompt = f"{instruccion_base}{texto_historial}{nota_vision}\n\nMensaje actual del prospecto: '{texto_recibido}'"

            parts_payload = [{"text": prompt}] + partes_adjuntas
            payload_gemini = {
                "contents": [{"parts": parts_payload}],
                "generationConfig": {"temperature": 0.4},
            }

            res = None
            for intento in range(2):
                try:
                    res = requests.post(url_gemini, headers=headers_gemini, json=payload_gemini, timeout=(10, 30))
                    if res.status_code == 200:
                        break
                    log(f"[GEMINI ERROR] Intento {intento + 1} HTTP {res.status_code}: {res.text[:250]}")
                    if res.status_code in (429, 500, 502, 503, 504) and intento == 0:
                        time.sleep(2.0)
                        continue
                    break
                except requests.exceptions.RequestException as e:
                    log(f"[GEMINI EXCEPCION] Intento {intento + 1}: {e}")
                    if intento == 0:
                        time.sleep(2.0)
                        continue

            # Respaldo automático: si falla con adjunto, reintenta solo con texto para no colgar el chat
            if (res is None or res.status_code != 200) and partes_adjuntas:
                log("[GEMINI] Reintentando solo con texto tras fallo con adjunto.")
                payload_fallback = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.4},
                }
                res = requests.post(url_gemini, headers=headers_gemini, json=payload_fallback, timeout=(10, 25))

            if res is not None and res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates") or []
                parts = (candidates[0].get("content", {}).get("parts") or []) if candidates else []
                texto_generado = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()

                if texto_generado:
                    respuesta_final = texto_generado
                    gemini_ok = True
                    # Salvavidas de video de bienvenida: si Gemini olvidó la etiqueta [VIDEO:...], la inyecta
                    if "video de arriba" in respuesta_final.lower() and "[VIDEO:" not in respuesta_final.upper():
                        respuesta_final = f"[VIDEO:{URL_VIDEO_GANCHO}]\n{respuesta_final}"
                        log(f"[SALVAVIDAS VIDEO] Etiqueta de video-gancho inyectada para {numero_remitente}")
                else:
                    log(f"[GEMINI AVISO] Respuesta vacía: {str(data)[:250]}")

                # Si pausaste a Carla justo mientras Gemini pensaba, no envía nada
                if esta_bot_pausado(numero_remitente):
                    return

                # --- INTERCEPTOR ANTI-DOBLE RESPUESTA (BLINDADO) ---
                with semaforo:
                    if numero_remitente in buzon_mensajes and buzon_mensajes[numero_remitente]:
                        buzon_mensajes[numero_remitente].insert(0, texto_recibido)
                        if media_recibida:
                            buzon_media.setdefault(numero_remitente, [])
                            buzon_media[numero_remitente] = media_recibida + buzon_media[numero_remitente]
                        if numero_remitente not in timers or not timers[numero_remitente].is_alive():
                            nuevo_timer = threading.Timer(
                                ESPERA_SEGUNDOS, despachar_buzon, args=[numero_remitente]
                            )
                            nuevo_timer.daemon = True
                            timers[numero_remitente] = nuevo_timer
                            nuevo_timer.start()
                        return

                    memoria_actual = list(historial_conversaciones.get(numero_remitente, []))
                    memoria_actual.append(f"Cliente: {texto_recibido}")
                    memoria_actual.append(f"Tú: {respuesta_final}")
                    historial_conversaciones[numero_remitente] = memoria_actual[-12:]
        except Exception:
            log(f"[GEMINI ERROR GENERAL] {traceback.format_exc()}")
    else:
        log("[GEMINI ERROR] GEMINI_API_KEY no configurada.")

    if esta_bot_pausado(numero_remitente):
        return

    try:
        patron_multimedia = r'\[(VIDEO|IMAGE|DOCUMENT|AUDIO):([^\]]+)\]'

        if re.search(patron_multimedia, respuesta_final, flags=re.IGNORECASE):
            partes = re.split(patron_multimedia, respuesta_final, flags=re.IGNORECASE)
            texto_inicial = partes[0].strip()

            bloques_media = []
            for idx in range(1, len(partes), 3):
                tipo_media = partes[idx].strip()
                url_archivo = partes[idx + 1].strip()
                texto_debajo = partes[idx + 2].strip() if (idx + 2) < len(partes) else ""
                bloques_media.append([tipo_media, url_archivo, texto_debajo])

            # Salvavidas 1: Si envía un solo archivo y puso el texto arriba dejando vacío abajo, lo une debajo del archivo
            if len(bloques_media) == 1 and texto_inicial and not bloques_media[0][2]:
                bloques_media[0][2] = texto_inicial
                texto_inicial = ""

            # Salvavidas 2: Si envía 2 archivos seguidos y dejó los párrafos juntos al final, los reparte en orden
            if len(bloques_media) >= 2 and not bloques_media[0][2] and bloques_media[-1][2]:
                parrafos_finales = [p.strip() for p in re.split(r'\n\s*\n', bloques_media[-1][2]) if p.strip()]
                if len(parrafos_finales) >= len(bloques_media):
                    for i in range(len(bloques_media) - 1):
                        bloques_media[i][2] = parrafos_finales[i]
                    bloques_media[-1][2] = "\n\n".join(parrafos_finales[len(bloques_media) - 1:])

            # 1. Si hay texto introductorio (como el precio arriba en el Paso 4), lo envía primero en su burbuja
            if texto_inicial:
                enviar_mensaje_whatsapp(numero_remitente, texto_inicial)
                time.sleep(2.5)

            # 2. Envía cada archivo con su propio texto debajo y 4 segundos entre archivos para que Meta respete el orden
            total_archivos = len(bloques_media)
            for i, (tipo_media, url_archivo, caption_actual) in enumerate(bloques_media):
                nombre_archivo = url_archivo.split("/")[-1]
                es_ultimo = (i == total_archivos - 1)

                exito = enviar_multimedia_whatsapp(
                    numero_remitente,
                    tipo_media,
                    url_archivo,
                    nombre_archivo,
                    caption=caption_actual,
                )

                if not exito and caption_actual:
                    enviar_mensaje_whatsapp(numero_remitente, caption_actual)

                if not es_ultimo:
                    time.sleep(4.0)
        else:
            texto_limpio = respuesta_final.strip()
            if texto_limpio:
                enviar_mensaje_whatsapp(numero_remitente, texto_limpio)

        # Registrar respuesta de Carla en el Panel (solo RAM)
        etapa_auto = "Link enviado" if "go.hotmart.com" in respuesta_final else None
        registrar_interaccion(
            telefono=numero_remitente,
            remitente="CARLA",
            texto=limpiar_etiquetas_para_panel(respuesta_final),
            tipo="text",
            forzar_etapa=etapa_auto,
        )

        # Programar o actualizar reloj de remarketing de 2 horas (solo tras precio o link)
        if gemini_ok:
            actualizar_y_programar_remarketing(numero_remitente, respuesta_final)
    except Exception:
        log(f"[ENVIO ERROR GENERAL] {traceback.format_exc()}")


def despachar_buzon(numero_remitente):
    global buzon_mensajes, buzon_media, timers, candados_clientes

    with semaforo:
        if numero_remitente not in candados_clientes:
            candados_clientes[numero_remitente] = threading.Lock()
        candado_cliente = candados_clientes[numero_remitente]

    if not candado_cliente.acquire(timeout=45):
        log(f"[DESPACHAR ERROR] Timeout esperando candado para {numero_remitente}")
        return

    try:
        with semaforo:
            if numero_remitente in buzon_mensajes and buzon_mensajes[numero_remitente]:
                textos_agrupados = " ".join(buzon_mensajes[numero_remitente])
                media_agrupada = list(buzon_media.get(numero_remitente, []))

                del buzon_mensajes[numero_remitente]
                buzon_media.pop(numero_remitente, None)
                timers.pop(numero_remitente, None)
            else:
                return

        if esta_bot_pausado(numero_remitente):
            return

        procesar_mensaje_gemini(numero_remitente, textos_agrupados, media_agrupada)
    except Exception:
        log(f"[DESPACHAR EXCEPCION] {traceback.format_exc()}")
    finally:
        candado_cliente.release()


# --- RUTAS PRINCIPALES Y WEBHOOK ---
@app.route("/", methods=["GET"])
def home():
    return "Servidor activo, conectado a WhatsApp y Gemini 3.5 Flash-Lite.", 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    global buzon_mensajes, buzon_media, timers, mensajes_procesados

    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        else:
            return "Forbidden", 403

    elif request.method == "POST":
        try:
            body = request.get_json(silent=True) or {}

            if body.get("object") == "whatsapp_business_account":
                for entry in body.get("entry", []):
                    for change in entry.get("changes", []):
                        value = change.get("value", {})

                        if "statuses" in value:
                            continue

                        if "messages" in value:
                            contacts = value.get("contacts", [])
                            nombres_por_id = {
                                c.get("wa_id"): c.get("profile", {}).get("name", "")
                                for c in contacts
                                if isinstance(c, dict)
                            }
                            nombre_default = contacts[0].get("profile", {}).get("name", "") if contacts else ""

                            for mensaje in value["messages"]:
                                id_mensaje = mensaje.get("id")
                                numero_remitente = mensaje["from"]
                                tipo_msg = mensaje.get("type", "")
                                nombre_contacto = nombres_por_id.get(numero_remitente, nombre_default)

                                # Deduplicación segura para todos los tipos de mensaje
                                with semaforo:
                                    if id_mensaje:
                                        if id_mensaje in mensajes_procesados:
                                            continue
                                        mensajes_procesados.add(id_mensaje)
                                        if len(mensajes_procesados) > 1000:
                                            mensajes_procesados.clear()

                                texto_para_bot = None
                                texto_para_panel = ""
                                media_info = None
                                tipo_panel = "text"
                                media_id = ""

                                if tipo_msg == "text":
                                    texto_recibido = mensaje["text"]["body"]
                                    texto_para_bot = texto_recibido
                                    texto_para_panel = texto_recibido
                                    tipo_panel = "text"

                                elif tipo_msg == "image":
                                    img_obj = mensaje.get("image", {})
                                    media_id = img_obj.get("id", "")
                                    mime_type = img_obj.get("mime_type", "image/jpeg")
                                    caption = img_obj.get("caption", "").strip()
                                    tipo_panel = "image"
                                    texto_para_panel = caption if caption else "[Imagen enviada]"
                                    texto_para_bot = caption if caption else "[El cliente envió una imagen]"
                                    if media_id:
                                        media_info = {
                                            "media_id": media_id,
                                            "media_type": "image",
                                            "mime_type": mime_type,
                                        }

                                elif tipo_msg == "document":
                                    doc_obj = mensaje.get("document", {})
                                    media_id = doc_obj.get("id", "")
                                    mime_type = doc_obj.get("mime_type", "application/pdf")
                                    filename = doc_obj.get("filename", "documento")
                                    caption = doc_obj.get("caption", "").strip()
                                    tipo_panel = "document"
                                    texto_para_panel = caption if caption else f"[Documento: {filename}]"
                                    texto_para_bot = (
                                        f"[El cliente envió un documento: {filename}] {caption}".strip()
                                        if caption
                                        else f"[El cliente envió un documento: {filename}]"
                                    )
                                    if media_id:
                                        media_info = {
                                            "media_id": media_id,
                                            "media_type": "document",
                                            "mime_type": mime_type,
                                            "filename": filename,
                                        }

                                elif tipo_msg in ("audio", "video", "sticker"):
                                    registrar_actividad_cliente_rmk(numero_remitente, "")
                                    registrar_interaccion(
                                        telefono=numero_remitente,
                                        remitente="CLIENTE",
                                        texto=f"[{tipo_msg.capitalize()} recibido]",
                                        tipo=tipo_msg,
                                        nombre=nombre_contacto,
                                    )
                                    continue
                                else:
                                    continue

                                # 0. Cancelar al instante cualquier reloj de remarketing pendiente
                                registrar_actividad_cliente_rmk(numero_remitente, texto_para_bot or "")

                                # 1. Registrar siempre en el panel (RAM)
                                registrar_interaccion(
                                    telefono=numero_remitente,
                                    remitente="CLIENTE",
                                    texto=texto_para_panel,
                                    tipo=tipo_panel,
                                    media_id=media_id,
                                    nombre=nombre_contacto,
                                )

                                # 2. Si Carla está pausada manualmente en este chat, no programa respuesta
                                if esta_bot_pausado(numero_remitente):
                                    continue

                                # 3. Agrupar en el buzón con tu temporizador original de 5 segundos
                                if texto_para_bot:
                                    with semaforo:
                                        if numero_remitente not in buzon_mensajes:
                                            buzon_mensajes[numero_remitente] = []
                                        buzon_mensajes[numero_remitente].append(texto_para_bot)

                                        if media_info:
                                            if numero_remitente not in buzon_media:
                                                buzon_media[numero_remitente] = []
                                            buzon_media[numero_remitente].append(media_info)

                                        if numero_remitente in timers:
                                            timers[numero_remitente].cancel()

                                        nuevo_timer = threading.Timer(
                                            ESPERA_SEGUNDOS, despachar_buzon, args=[numero_remitente]
                                        )
                                        nuevo_timer.daemon = True
                                        timers[numero_remitente] = nuevo_timer
                                        nuevo_timer.start()

        except Exception:
            log(f"[WEBHOOK EXCEPCION] {traceback.format_exc()}")

        return jsonify({"status": "success"}), 200


# --- RUTAS PRIVADAS DE LA MINI WEB (/panel) ---
def requiere_panel_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("panel_auth"):
            return jsonify({"ok": False, "error": "No autorizado"}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.route("/panel", methods=["GET"])
def panel_view():
    return render_template("panel.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    if not PANEL_PASSWORD:
        return jsonify({"ok": False, "error": "Falta configurar PANEL_PASSWORD en Render."}), 401
    data = request.get_json(silent=True) or {}
    pwd = str(data.get("password", "")).strip()
    if secrets.compare_digest(pwd, PANEL_PASSWORD):
        session["panel_auth"] = True
        return jsonify({"ok": True}), 200
    return jsonify({"ok": False, "error": "Contraseña incorrecta."}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True}), 200


@app.route("/api/chats", methods=["GET"])
@requiere_panel_auth
def api_chats():
    with semaforo:
        lista = sorted(
            clientes_panel.values(),
            key=lambda c: c.get("_orden", 0),
            reverse=True,
        )
        chats_limpios = [
            {
                "telefono": c["telefono"],
                "nombre": c["nombre"],
                "estado_bot": c["estado_bot"],
                "etapa": c["etapa"],
                "ultimo_mensaje": c["ultimo_mensaje"],
                "ultima_fecha": c["ultima_fecha"],
                "notas": c["notas"],
                "no_leidos": c.get("no_leidos", 0),
            }
            for c in lista
        ]
    return jsonify({"ok": True, "chats": chats_limpios}), 200


@app.route("/api/chat/<telefono>", methods=["GET"])
@requiere_panel_auth
def api_chat_detalle(telefono):
    with semaforo:
        cli = clientes_panel.get(telefono)
        if not cli:
            return jsonify({"ok": False, "error": "Cliente no encontrado"}), 404
        cli["no_leidos"] = 0
        cliente_out = {
            "telefono": cli["telefono"],
            "nombre": cli["nombre"],
            "estado_bot": cli["estado_bot"],
            "etapa": cli["etapa"],
            "ultimo_mensaje": cli["ultimo_mensaje"],
            "ultima_fecha": cli["ultima_fecha"],
            "notas": cli["notas"],
        }
        msgs_out = list(mensajes_panel.get(telefono, []))
    return jsonify({"ok": True, "cliente": cliente_out, "mensajes": msgs_out}), 200


@app.route("/api/toggle_bot", methods=["POST"])
@requiere_panel_auth
def api_toggle_bot():
    data = request.get_json(silent=True) or {}
    telefono = str(data.get("telefono", "")).strip()
    nuevo_estado = str(data.get("estado_bot", "ACTIVO")).strip().upper()
    if nuevo_estado not in ("ACTIVO", "PAUSADO"):
        return jsonify({"ok": False, "error": "Estado inválido"}), 400

    with semaforo:
        cli = clientes_panel.get(telefono)
        if not cli:
            return jsonify({"ok": False, "error": "Cliente no encontrado"}), 404
        cli["estado_bot"] = nuevo_estado
        if nuevo_estado == "PAUSADO":
            if telefono in timers:
                timers[telefono].cancel()
                del timers[telefono]
            _cancelar_timer_remarketing_locked(telefono)
            buzon_mensajes.pop(telefono, None)
            buzon_media.pop(telefono, None)

    return jsonify({"ok": True, "estado_bot": nuevo_estado}), 200


@app.route("/api/update_client", methods=["POST"])
@requiere_panel_auth
def api_update_client():
    data = request.get_json(silent=True) or {}
    telefono = str(data.get("telefono", "")).strip()
    etapa = str(data.get("etapa", "Nuevo")).strip()
    notas = str(data.get("notas", "")).strip()

    with semaforo:
        cli = clientes_panel.get(telefono)
        if not cli:
            return jsonify({"ok": False, "error": "Cliente no encontrado"}), 404
        cli["etapa"] = etapa
        cli["notas"] = notas

        etapa_low = etapa.lower()
        if any(k in etapa_low for k in ("pagado", "venta", "compr", "cerrado", "alumn")):
            _cancelar_timer_remarketing_locked(telefono)
            est = _obtener_estado_rmk_locked(telefono)
            est["pago_confirmado"] = True

    return jsonify({"ok": True}), 200


@app.route("/api/send", methods=["POST"])
@requiere_panel_auth
def api_send_manual():
    data = request.get_json(silent=True) or {}
    telefono = str(data.get("telefono", "")).strip()
    texto = str(data.get("texto", "")).strip()
    if not telefono or not texto:
        return jsonify({"ok": False, "error": "Falta teléfono o mensaje"}), 400

    exito = enviar_mensaje_whatsapp(telefono, texto)
    if not exito:
        return jsonify({
            "ok": False,
            "error": "Meta no permitió el envío (verifica si pasaron más de 24h desde el último mensaje del cliente).",
        }), 400

    # Al intervenir tú manualmente, Carla se pausa en este chat, cancela remarketing y guarda tu mensaje en memoria
    with semaforo:
        cli = clientes_panel.get(telefono)
        if cli:
            cli["estado_bot"] = "PAUSADO"
        if telefono in timers:
            timers[telefono].cancel()
            del timers[telefono]
        _cancelar_timer_remarketing_locked(telefono)
        buzon_mensajes.pop(telefono, None)
        buzon_media.pop(telefono, None)

        memoria_actual = list(historial_conversaciones.get(telefono, []))
        memoria_actual.append(f"Tú: {texto}")
        historial_conversaciones[telefono] = memoria_actual[-12:]

    registrar_interaccion(
        telefono=telefono,
        remitente="HUMANO",
        texto=texto,
        tipo="text",
    )
    return jsonify({"ok": True}), 200


@app.route("/api/media/<media_id>", methods=["GET"])
@requiere_panel_auth
def api_media(media_id):
    contenido, mime_type = descargar_media_whatsapp(media_id)
    if not contenido:
        return "Archivo no disponible o expirado en Meta.", 404
    return Response(contenido, mimetype=mime_type or "application/octet-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
