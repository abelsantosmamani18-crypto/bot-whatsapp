import os
import re
import time
import base64
import secrets
import threading
import requests
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, Response

app = Flask(__name__)

# --- VARIABLES DE ENTORNO EXISTENTES (INTACTAS) ---
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "bot_melamina_cejas_2026")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- VARIABLES NUEVAS (MINI WEB + GOOGLE SHEETS) ---
SHEETS_WEBHOOK_URL = os.environ.get("SHEETS_WEBHOOK_URL", "").strip()
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "").strip()

app.secret_key = os.environ.get("FLASK_SECRET_KEY") or f"{PANEL_PASSWORD}_creas_y_aprendes_secret_2026"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

TZ_PERU = timezone(timedelta(hours=-5))
MIME_SOPORTADOS_GEMINI = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}
MAX_BYTES_GEMINI = 5 * 1024 * 1024  # 5 MB máximo para proteger RAM en Render

# --- MEMORIA RAM Y BUZÓN TEMPORAL ---
historial_conversaciones = {}
buzon_mensajes = {}
buzon_media = {}
timers = {}
candados_clientes = {}
mensajes_procesados = set()
ESPERA_SEGUNDOS = 5.0

# --- MEMORIA DEL PANEL EN VIVO ---
clientes_panel = {}
mensajes_panel = {}

# --- EL CANDADO DE SEGURIDAD ---
semaforo = threading.Lock()


def ahora_peru():
    return datetime.now(TZ_PERU).strftime("%d/%m/%Y %H:%M")


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


# --- SINCRONIZACIÓN ASÍNCRONA CON GOOGLE SHEETS (NUNCA FRENA A CARLA) ---
def _post_sheets(payload):
    if not SHEETS_WEBHOOK_URL:
        return
    try:
        requests.post(SHEETS_WEBHOOK_URL, json=payload, timeout=15)
    except Exception as e:
        print(f"Aviso Sheets (segundo plano): {e}")


def enviar_a_sheets_async(payload):
    if not SHEETS_WEBHOOK_URL:
        return
    threading.Thread(target=_post_sheets, args=(payload,), daemon=True).start()


def registrar_interaccion(telefono, remitente, texto, tipo="text", media_id="", nombre="", forzar_etapa=None):
    """Guarda en la memoria del panel y dispara guardado en segundo plano a Google Sheets."""
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

        payload_sheets = {
            "accion": "registrar_mensaje",
            "fecha_hora": fecha,
            "telefono": telefono,
            "nombre": cli["nombre"],
            "remitente": remitente_norm,
            "tipo": tipo,
            "contenido": texto,
            "media_ref": media_id,
            "estado_bot": cli["estado_bot"],
            "etapa": cli["etapa"],
            "ultimo_mensaje": cli["ultimo_mensaje"],
            "ultima_fecha": fecha,
            "notas": cli["notas"],
        }

    enviar_a_sheets_async(payload_sheets)


def cargar_desde_sheets():
    """Recupera clientes y últimos mensajes al arrancar el servidor en Render."""
    if not SHEETS_WEBHOOK_URL:
        return
    try:
        res = requests.get(SHEETS_WEBHOOK_URL, timeout=15)
        if res.status_code != 200:
            return
        data = res.json()
        if not data.get("ok"):
            return

        clientes_cargados = data.get("clientes", [])
        mensajes_cargados = data.get("mensajes", [])

        with semaforo:
            base_orden = time.time() - 100000
            for idx, c in enumerate(clientes_cargados):
                tel = str(c.get("telefono", "")).strip()
                if not tel:
                    continue
                if tel not in clientes_panel:
                    clientes_panel[tel] = {
                        "telefono": tel,
                        "nombre": str(c.get("nombre", "") or tel),
                        "estado_bot": str(c.get("estado_bot", "ACTIVO") or "ACTIVO").upper(),
                        "etapa": str(c.get("etapa", "Nuevo") or "Nuevo"),
                        "ultimo_mensaje": str(c.get("ultimo_mensaje", "") or ""),
                        "ultima_fecha": str(c.get("ultima_fecha", "") or ""),
                        "notas": str(c.get("notas", "") or ""),
                        "no_leidos": 0,
                        "_orden": base_orden + idx,
                    }

            msgs_por_tel = {}
            hist_por_tel = {}
            for idx, m in enumerate(mensajes_cargados):
                tel = str(m.get("telefono", "")).strip()
                if not tel:
                    continue
                rem_raw = str(m.get("remitente", "CLIENTE")).strip().upper()
                if rem_raw not in ("CLIENTE", "CARLA", "HUMANO"):
                    rem_raw = "CLIENTE"
                tipo = str(m.get("tipo", "text") or "text")
                contenido = str(m.get("contenido", "") or "")
                media_ref = str(m.get("media_ref", "") or "")
                fecha_h = str(m.get("fecha_hora", "") or "")

                msgs_por_tel.setdefault(tel, []).append({
                    "remitente": rem_raw,
                    "texto": contenido,
                    "fecha": fecha_h,
                    "media_id": media_ref,
                    "media_type": tipo if media_ref else "text",
                })

                prefijo = "Cliente" if rem_raw == "CLIENTE" else "Tú"
                if contenido:
                    hist_por_tel.setdefault(tel, []).append(f"{prefijo}: {contenido}")

                if tel in clientes_panel:
                    clientes_panel[tel]["_orden"] = max(clientes_panel[tel]["_orden"], base_orden + 10000 + idx)

            for tel, lista_m in msgs_por_tel.items():
                actuales = mensajes_panel.get(tel, [])
                mensajes_panel[tel] = (lista_m + actuales)[-150:]

            for tel, lista_h in hist_por_tel.items():
                if tel not in historial_conversaciones:
                    historial_conversaciones[tel] = lista_h[-12:]

        print(f"Sheets cargado: {len(clientes_cargados)} clientes, {len(mensajes_cargados)} mensajes.")
    except Exception as e:
        print(f"Aviso cargando Sheets al inicio: {e}")


# Carga inicial en segundo plano para no demorar el arranque de Gunicorn
threading.Thread(target=cargar_desde_sheets, daemon=True).start()


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
            timeout=10,
        )
        meta_res.raise_for_status()
        meta_data = meta_res.json()
        media_url = meta_data.get("url")
        mime_type = meta_data.get("mime_type", "application/octet-stream")
        if not media_url:
            return None, None

        file_res = requests.get(media_url, headers=headers, timeout=15)
        file_res.raise_for_status()
        return file_res.content, mime_type
    except Exception as e:
        print(f"Error descargando media de Meta ({media_id}): {e}")
        return None, None


def enviar_mensaje_whatsapp(telefono_destino, texto_mensaje):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
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
        respuesta = requests.post(url, headers=headers, json=payload, timeout=15)
        respuesta.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error enviando mensaje a Meta: {e}")
        return False


def enviar_multimedia_whatsapp(telefono_destino, tipo_media, url_archivo, nombre_archivo="", caption=""):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
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
        respuesta = requests.post(url, headers=headers, json=payload, timeout=20)
        respuesta.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error enviando multimedia a Meta: {e}")
        return False


# --- CEREBRO DE CARLA (GEMINI 3.5 FLASH-LITE + VISIÓN DE IMÁGENES Y PDF) ---
def procesar_mensaje_gemini(numero_remitente, texto_recibido, media_recibida=None):
    global historial_conversaciones

    if esta_bot_pausado(numero_remitente):
        return

    respuesta_final = "Lo siento, tengo un problema técnico temporal."

    if GEMINI_API_KEY:
        try:
            url_gemini = f"https://generativelanguage.googleapis.com/v1/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
            headers_gemini = {"Content-Type": "application/json"}

            try:
                with open("prompt_ventas.txt", "r", encoding="utf-8") as archivo:
                    instruccion_base = archivo.read().strip()
            except Exception:
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
            payload_gemini = {"contents": [{"parts": parts_payload}]}

            res = requests.post(url_gemini, headers=headers_gemini, json=payload_gemini, timeout=30)

            # Respaldo automático: si falla con adjunto, reintenta solo con texto para no colgar el chat
            if res.status_code != 200 and partes_adjuntas:
                print(f"Aviso Gemini con adjunto ({res.status_code}), reintentando solo texto.")
                payload_fallback = {"contents": [{"parts": [{"text": prompt}]}]}
                res = requests.post(url_gemini, headers=headers_gemini, json=payload_fallback, timeout=25)

            if res.status_code == 200:
                data = res.json()
                respuesta_final = data["candidates"][0]["content"]["parts"][0]["text"]

                # Si pausaste a Carla justo mientras Gemini pensaba, no envía nada
                if esta_bot_pausado(numero_remitente):
                    return

                # --- INTERCEPTOR ANTI-DOBLE RESPUESTA (INTACTO) ---
                with semaforo:
                    if numero_remitente in buzon_mensajes and buzon_mensajes[numero_remitente]:
                        buzon_mensajes[numero_remitente].insert(0, texto_recibido)
                        if media_recibida:
                            buzon_media.setdefault(numero_remitente, [])
                            buzon_media[numero_remitente] = media_recibida + buzon_media[numero_remitente]
                        return

                    memoria_actual = list(historial_conversaciones.get(numero_remitente, []))
                    memoria_actual.append(f"Cliente: {texto_recibido}")
                    memoria_actual.append(f"Tú: {respuesta_final}")
                    historial_conversaciones[numero_remitente] = memoria_actual[-12:]
        except Exception as e:
            print(f"Error en la conexión a Gemini: {e}")

    if esta_bot_pausado(numero_remitente):
        return

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

    # Registrar respuesta de Carla en el Panel y Google Sheets
    etapa_auto = "Link enviado" if "go.hotmart.com" in respuesta_final else None
    registrar_interaccion(
        telefono=numero_remitente,
        remitente="CARLA",
        texto=limpiar_etiquetas_para_panel(respuesta_final),
        tipo="text",
        forzar_etapa=etapa_auto,
    )


def despachar_buzon(numero_remitente):
    global buzon_mensajes, buzon_media, timers, candados_clientes

    with semaforo:
        if numero_remitente not in candados_clientes:
            candados_clientes[numero_remitente] = threading.Lock()
        candado_cliente = candados_clientes[numero_remitente]

    with candado_cliente:
        with semaforo:
            if numero_remitente in buzon_mensajes and buzon_mensajes[numero_remitente]:
                textos_agrupados = " ".join(buzon_mensajes[numero_remitente])
                media_agrupada = list(buzon_media.get(numero_remitente, []))

                del buzon_mensajes[numero_remitente]
                if numero_remitente in buzon_media:
                    del buzon_media[numero_remitente]
                if numero_remitente in timers:
                    del timers[numero_remitente]
            else:
                return

        if esta_bot_pausado(numero_remitente):
            return

        procesar_mensaje_gemini(numero_remitente, textos_agrupados, media_agrupada)


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
            body = request.get_json()

            if body.get("object") == "whatsapp_business_account":
                for entry in body.get("entry", []):
                    for change in entry.get("changes", []):
                        value = change.get("value", {})

                        if "statuses" in value:
                            continue

                        if "messages" in value:
                            mensaje = value["messages"][0]
                            id_mensaje = mensaje.get("id")
                            numero_remitente = mensaje["from"]
                            tipo_msg = mensaje.get("type", "")

                            contacts = value.get("contacts", [])
                            nombre_contacto = ""
                            if contacts:
                                nombre_contacto = contacts[0].get("profile", {}).get("name", "")

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
                                # Solo lo registra en el panel y Sheets para que no quedes a ciegas
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

                            # 1. Registrar siempre en el panel y Google Sheets
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

                                    timers[numero_remitente] = threading.Timer(
                                        ESPERA_SEGUNDOS, despachar_buzon, args=[numero_remitente]
                                    )
                                    timers[numero_remitente].start()

        except Exception as e:
            print(f"Error general en el webhook: {e}")

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
            buzon_mensajes.pop(telefono, None)
            buzon_media.pop(telefono, None)

        payload_sheets = {
            "accion": "actualizar_cliente",
            "telefono": telefono,
            "estado_bot": nuevo_estado,
        }

    enviar_a_sheets_async(payload_sheets)
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
        payload_sheets = {
            "accion": "actualizar_cliente",
            "telefono": telefono,
            "etapa": etapa,
            "notas": notas,
        }

    enviar_a_sheets_async(payload_sheets)
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

    # Al intervenir tú manualmente, Carla se pausa en este chat y guarda tu mensaje en su memoria
    with semaforo:
        cli = clientes_panel.get(telefono)
        if cli:
            cli["estado_bot"] = "PAUSADO"
        if telefono in timers:
            timers[telefono].cancel()
            del timers[telefono]
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
