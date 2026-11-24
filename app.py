import os
import re
import sys
import time
import base64
import secrets
import threading
import traceback
import unicodedata
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

# --- ARCHIVO ÚNICO DE PROMPT CON SEPARADORES INTERNOS ---
ARCHIVO_PROMPT = "prompt_ventas.txt"
PATRON_SEPARADORES = re.compile(
    r"^\s*===\s*(ANEXO_TEMARIO|ANEXO_PAGOS|ANEXO_POSTPAGO)\s*===\s*$",
    flags=re.MULTILINE | re.IGNORECASE,
)
MAPA_SEPARADORES = {
    "ANEXO_TEMARIO": "temario",
    "ANEXO_PAGOS": "pagos",
    "ANEXO_POSTPAGO": "postpago",
}

# --- URLs MULTIMEDIA Y TEXTOS FIJOS DEL EMBUDO (PASOS 1 AL 4 GRATIS) ---
URL_VIDEO_GANCHO = "https://cdn.jsdelivr.net/gh/abelsantosmamani18-crypto/bot-whatsapp@main/static/velas_y_jabones/video-gancho.mp4"
URL_TEMARIO = "https://cdn.jsdelivr.net/gh/abelsantosmamani18-crypto/bot-whatsapp@main/static/velas_y_jabones/temario.pdf"
URL_CERTIFICADO = "https://cdn.jsdelivr.net/gh/abelsantosmamani18-crypto/bot-whatsapp@main/static/velas_y_jabones/certificado.png"
URL_PLATAFORMA = "https://cdn.jsdelivr.net/gh/abelsantosmamani18-crypto/bot-whatsapp@main/static/velas_y_jabones/video-plataforma.mp4"
URL_BONOS = "https://cdn.jsdelivr.net/gh/abelsantosmamani18-crypto/bot-whatsapp@main/static/velas_y_jabones/bonos.pdf"

CATALOGO_ARCHIVOS_RESPALDO = {
    "temario.pdf": f"[DOCUMENT:{URL_TEMARIO}]",
    "certificado.png": f"[IMAGE:{URL_CERTIFICADO}]",
    "video-plataforma.mp4": f"[VIDEO:{URL_PLATAFORMA}]",
    "bonos.pdf": f"[DOCUMENT:{URL_BONOS}]",
}

TEXTO_PASO_1 = (
    f"[VIDEO:{URL_VIDEO_GANCHO}]\n"
    "¡Hola! Soy *Carla*, te doy la bienvenida a *Creas y Aprendes* 👋. "
    "En el video de arriba, nuestra instructora *Laura Salamanca* te muestra todo lo que lograrás crear con nuestro *programa 3x1* ✨.\n\n"
    "Cuéntame, ¿te gustaría aprender desde cero para *crear tu propia marca* de velas y jabones y tener un *negocio rentable* desde casa? 😊"
)

TEXTO_PASO_2 = (
    "¡Me alegra muchísimo! Aquí aprenderás *paso a paso desde cero*, con las medidas y temperaturas exactas "
    "para no desperdiciar materiales, hasta crear diseños profesionales y venderlos bajo tu propia marca ✨.\n\n"
    "¿Te gustaría que te envíe el *temario oficial* para que revises todos los módulos al detalle? 📋"
)

TEXTO_PASO_3 = (
    f"[DOCUMENT:{URL_TEMARIO}]\n"
    "👆📄 Arriba tienes el *temario oficial en PDF* con todos los módulos de los 3 programas (*Velas, Jabones y creación de tu Marca*) 📋.\n\n"
    f"[IMAGE:{URL_CERTIFICADO}]\n"
    "🎓 Y aquí ves el *Certificado Internacional* que recibirás con tu nombre al finalizar para respaldar tu negocio ✨.\n\n"
    "¿Tienes alguna consulta sobre los módulos o te paso el *precio promocional* junto con los *18 bonos* y la plataforma por dentro? 🎁"
)

TEXTO_PASO_4 = (
    "El valor normal de los *3 programas completos* (*Velas, Jabones y creación de tu Marca*) es de ~S/ 220.00~, "
    "pero hoy con el *50% de descuento* te llevas la *promoción 3x1* con acceso de por vida por un *único pago de S/ 110.00* 🔥.\n\n"
    f"[DOCUMENT:{URL_BONOS}]\n"
    "👆📄 Además, hoy desbloqueas gratis en tu plataforma *18 bonos exclusivos* (cursos extra en video HD, recetarios, proveedores y plantillas en Excel). "
    "Arriba tienes el catálogo completo 🎁.\n\n"
    f"[VIDEO:{URL_PLATAFORMA}]\n"
    "🎥 Aquí arriba ves nuestra *plataforma por dentro* para aprender desde cero en *video HD* a tu ritmo, sin desperdiciar materiales ✨.\n\n"
    "Hoy solo me quedan *3 cupos* con el *50% de descuento* del *3x1*, ¿tienes alguna consulta o te envío el *enlace oficial* para asegurar el tuyo? 🔥"
)

# --- ROTACIÓN AUTOMÁTICA DE ESCASEZ PARA CASO D (POST-PRECIO / PRE-LINK) ---
ROTACION_CASO_D = (
    ("¿Aprovechamos...", "el precio promocional de *S/ 110.00* (único pago de por vida) antes de que vuelva a ~S/ 220.00~", "🔥"),
    ("¿Aseguras...", "el *50% de descuento* activo hoy por tiempo limitado para recibir tu *enlace oficial*", "✨"),
    ("¿Arrancamos...", "hoy mismo con la oferta *3x1* (*3 programas completos* por el precio de 1)", "🎓"),
    ("¿Desbloqueamos...", "hoy gratis tus *18 bonos exclusivos* junto con tu acceso de por vida", "🎁"),
    ("¿Te animas a separar...", "tu lugar hoy sin riesgo respaldada por los *7 días de garantía* en Hotmart", "🛡️"),
    ("¿Separamos...", "tu ingreso hoy para arrancar acompañada desde casa en la *Comunidad VIP*", "📲"),
    ("¿Quieres asegurar...", "uno de los últimos *3 cupos de hoy* antes de que cierre la promoción", "📩"),
)

# --- REGLAS DE FILTRO CONSERVADOR (PASOS GRATIS) Y DISPARADORES DE ANEXOS ---
BLOQUEO_PASO_1_FRASES = (
    "precio", "cuanto", "cuesta", "costo", "valor", "presencial", "donde",
    "ubicacion", "direccion", "horario", "yape", "plin", "pago", "pagar",
    "tarjeta", "efectivo", "paypal", "cuota", "material", "insumo", "certificado",
    "diploma", "link", "enlace", "garantia", "tiempo", "duracion", "gratis",
    "bono", "temario", "modulo", "hotmart", "pague", "compre", "estafa", "confiable",
    "seguro", "como funciona", "como es", "como llega", "que incluye", "incluye",
    "de que trata", "que es", "voucher", "comprobante", "correo", "error", "ayuda",
)

BLOQUEO_PASO_1_PALABRAS = {"vale", "hora", "horas", "dura", "duran"}

SALUDOS_PASO_1 = (
    "hola", "buenas", "buen dia", "buenos dias", "info", "informacion", "interesa",
    "quisiera", "quiero", "curso", "taller", "programa", "detalles", "saludos",
    "hey", "hi", "velas", "jabones", "3x1",
)

PALABRAS_AFIRMATIVAS = {
    "si", "sii", "siii", "siiii", "claro", "dale", "ok", "okay", "vale", "bueno",
    "genial", "perfecto", "encantaria", "gustaria", "quisiera", "quiero", "enviame",
    "enviamelo", "pasame", "pasamelo", "mandame", "mandamelo", "muestrame", "porfa",
    "favor", "obvio", "seguro", "listo", "va", "sip", "excelente", "super", "envialo",
    "pasalo", "mandalo",
}

PALABRAS_BLOQUEO_CONFIRMACION = {
    "no", "pero", "aunque", "cuanto", "cuesta", "vale", "presencial", "donde",
    "como", "cuando", "quien", "porque", "duda", "dudas", "consulta", "consultas",
    "pregunta", "preguntas", "yape", "plin", "tarjeta", "pago", "pagar", "caro",
    "dinero", "plata", "pensar", "luego", "despues", "material", "materiales",
    "horario", "horarios", "tiempo", "garantia", "link", "enlace", "gratis", "ya",
    "pague", "compre", "correo", "hotmart", "solo", "error", "ayuda", "voucher",
}

DISPARADORES_TEMARIO = (
    "temario", "modulo", "bono", "receta", "vela", "jabon", "cera", "glicerina",
    "molde", "yeso", "alabastro", "sosa", "saponif", "shampoo", "champu", "bomba",
    "balsamo", "difusor", "desmaquill", "navid", "grinch", "adviento", "halloween",
    "carbon", "avena", "cafe", "lufa", "arroz", "chocolate", "manzanilla", "curcuma",
    "lavanda", "romero", "calendula", "aloe", "jelly", "afrodis", "mascota", "bebe",
    "infantil", "canva", "capcut", "excel", "finanza", "negocio", "marca", "redes",
    "reel", "google maps", "material", "insumo", "provee", "capital", "inversion",
    "recuper", "ganan", "rentab", "maquina", "microonda", "gramera", "alcohol",
    "fragancia", "aroma", "pabilo", "colorante", "contenido", "aprender", "incluye",
    "ensen", "clase", "leccion", "laura", "andres", "instructor", "profesor", "manitas",
)

DISPARADORES_PAGOS = (
    "yap", "plin", "tarjet", "debit", "credit", "cuota", "pagoefectivo", "pago efectivo",
    "efectivo", "banco", "interbank", "bbva", "bcp", "scotiabank", "banbif",
    "caja", "tambo", "kasnet", "agente", "paypal", "pagar", "pago", "pagos", "medio",
    "metodo", "cupon", "109", "189", "aromaterapia", "arcilla", "polimeric", "casilla",
    "marcar", "captura", "pantallazo", "aprob", "codigo", "confirmar",
    "solicitar", "boton", "error", "rechaz", "fallo", "falla", "no puedo", "no me deja",
    "trabo", "trabado", "limite", "saldo", "checkout", "formulario", "celular",
    "matricul", "inscrib", "link", "enlace", "comprar", "deposito", "transfer",
)

DISPARADORES_PAGOS_PALABRAS = {"cip", "dni"}

DISPARADORES_POSTPAGO = (
    "pague", "pagado", "ya hice", "ya cancel", "ya yape", "ya deposit", "ya transfer",
    "ya compr", "ya me inscrib", "voucher", "baucher", "comprobante", "recibo",
    "constancia", "pedido recibido", "correo", "email", "mail", "gmail",
    "spam", "bandeja", "no me llega", "no llega", "hotmart", "aplicacion",
    "play store", "app store", "acceder", "acceso", "entrar", "ingresar", "ingreso",
    "mis compras", "contrasena", "clave", "6 digitos", "digitos",
    "codigo de 6", "codigo de seguridad", "codigo de verificacion", "codigo al correo",
    "verificar", "verificacion", "grupo", "vip", "comunidad", "descripcion",
    "equivoque", "mal escrito", "cambiar correo", "cambio de correo",
)

DISPARADORES_POSTPAGO_PALABRAS = {"cip", "app", "hp"}

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

# --- EL CANDADO DE SEGURIDAD (RLock reentrante para evitar bloqueos) ---
semaforo = threading.RLock()


def ahora_peru():
    return datetime.now(TZ_PERU).strftime("%d/%m/%Y %H:%M")


def log(mensaje):
    """Imprime directo a stderr con flush inmediato para visibilidad real en Render."""
    print(f"[{datetime.now(TZ_PERU).strftime('%H:%M:%S')}] {mensaje}", file=sys.stderr, flush=True)


def normalizar_texto(texto):
    """Convierte a minúsculas y quita tildes para comparaciones tolerantes a errores."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip()


def leer_archivo_texto(ruta, fallback=""):
    try:
        with open(ruta, "r", encoding="utf-8") as archivo:
            return archivo.read().strip()
    except Exception:
        return fallback


def cargar_prompt_modular():
    """
    Lee `prompt_ventas.txt` y lo separa en memoria en:
    nucleo + temario + pagos + postpago usando las líneas ===ANEXO_...===.
    Si falta algún separador, avisa en los logs de Render y sigue funcionando con respaldo.
    """
    contenido = leer_archivo_texto(ARCHIVO_PROMPT)
    if not contenido:
        log(f"[PROMPT ERROR] {ARCHIVO_PROMPT} no encontrado o vacío.")
        return {
            "nucleo": "Eres Carla, asesora de ventas de Creas y Aprendes.",
            "temario": "",
            "pagos": "",
            "postpago": "",
        }

    secciones = {"nucleo": "", "temario": "", "pagos": "", "postpago": ""}
    partes = PATRON_SEPARADORES.split(contenido)
    secciones["nucleo"] = partes[0].strip()

    for idx in range(1, len(partes), 2):
        clave_sep = partes[idx].strip().upper()
        bloque = partes[idx + 1].strip() if (idx + 1) < len(partes) else ""
        nombre_seccion = MAPA_SEPARADORES.get(clave_sep)
        if nombre_seccion:
            secciones[nombre_seccion] = bloque

    if len(partes) == 1:
        log(f"[PROMPT AVISO] No se detectaron separadores ===ANEXO_...=== en {ARCHIVO_PROMPT}; usando archivo completo.")
    else:
        faltantes = [k for k in ("temario", "pagos", "postpago") if not secciones[k]]
        if faltantes:
            log(f"[PROMPT AVISO] Secciones vacías o sin separador en {ARCHIVO_PROMPT}: {faltantes}")

    if not secciones["nucleo"]:
        secciones["nucleo"] = contenido

    return secciones


def limpiar_etiquetas_para_panel(texto):
    if not texto:
        return ""
    def _reemplazar(match):
        tipo = match.group(1).upper()
        archivo = match.group(2).strip().split("/")[-1]
        return f"[{tipo}: {archivo}]"
    return re.sub(r'\[(VIDEO|IMAGE|DOCUMENT|AUDIO):([^\]]+)\]', _reemplazar, texto, flags=re.IGNORECASE).strip()


def compactar_para_historial(texto):
    """Reemplaza URLs largas de multimedia por notas simples sin corchetes para ahorrar tokens sin que la IA las imite."""
    if not texto:
        return ""
    def _reemplazar(match):
        archivo = match.group(2).strip().split("/")[-1]
        return f"(archivo ya enviado antes: {archivo})"
    return re.sub(r'\[(VIDEO|IMAGE|DOCUMENT|AUDIO):([^\]]+)\]', _reemplazar, texto, flags=re.IGNORECASE).strip()


def normalizar_etiquetas_multimedia(texto):
    """Salvavidas: convierte marcas de historial o nombres sin URL en la etiqueta real [TIPO:URL] para enviar el archivo."""
    if not texto:
        return ""

    def _reemplazar_marca(match):
        archivo = match.group(1).strip().lower()
        etiqueta = CATALOGO_ARCHIVOS_RESPALDO.get(archivo)
        if etiqueta:
            log(f"[SALVAVIDAS ARCHIVO] Etiqueta reconstruida automáticamente para {archivo}")
            return etiqueta
        return match.group(0)

    patron_respaldo = (
        r'(?:\[\s*(?:ARCHIVO\s+ENVIADO|DOCUMENT|IMAGE|VIDEO)\s*:\s*|\(\s*archivo\s+ya\s+enviado\s+antes\s*:\s*)'
        r'(bonos\.pdf|temario\.pdf|certificado\.png|video-plataforma\.mp4)'
        r'\s*(?:\]|\))'
    )
    return re.sub(patron_respaldo, _reemplazar_marca, texto, flags=re.IGNORECASE)


def limpiar_formato_whatsapp(texto):
    """Corrige automáticamente **negritas** a *negritas* y mueve emojis antes de '?' hacia después del '?'."""
    if not texto:
        return ""
    texto = re.sub(r'\*\*([^*\n]+?)\*\*', r'*\1*', texto)
    texto = re.sub(
        r'\s*([✨🔥🎁💳🛡️😊📋🎓📲🏦👇👋📄🎥🛍️📩💻])\s*\?',
        r'? \1',
        texto,
    )
    return texto


def obtener_nota_rotacion_caso_d(numero_remitente, memoria, media_recibida=None):
    """
    Si el cliente ya vio temario + precio de S/ 110.00 (Paso 4) y aún NO recibió el link ni pagó,
    devuelve una instrucción corta con la escasez exacta del turno para evitar cierres repetidos.
    """
    if not memoria or media_recibida:
        return ""

    with semaforo:
        est = estado_remarketing.get(numero_remitente, {})
        if est.get("link_enviado") or est.get("pago_confirmado"):
            return ""

    historial_low = "\n".join(memoria).lower()
    if "go.hotmart.com" in historial_low:
        return ""

    # Confirmar que ya pasó por temario y Paso 4 (precio + bonos/plataforma)
    if "temario.pdf" not in historial_low:
        return ""
    if not ("110.00" in historial_low and ("bonos.pdf" in historial_low or "video-plataforma.mp4" in historial_low)):
        return ""

    # Contar cuántas respuestas de Carla hubo DESPUÉS del mensaje del Paso 4
    idx_paso_4 = -1
    for i, item in enumerate(memoria):
        low = item.lower()
        if item.startswith("Tú:") and "110.00" in low and ("bonos.pdf" in low or "video-plataforma.mp4" in low):
            idx_paso_4 = i
            break

    if idx_paso_4 == -1:
        return ""

    turnos_post_precio = sum(1 for item in memoria[idx_paso_4 + 1:] if item.startswith("Tú:"))
    verbo, palanca, emoji = ROTACION_CASO_D[turnos_post_precio % len(ROTACION_CASO_D)]

    return (
        "[INSTRUCCIÓN INTERNA DE CIERRE — CASO D: "
        "Si el cliente NO está pidiendo el enlace ahora (Paso 5) ni diciendo 'lo voy a pensar', "
        f"cierra en el último párrafo aparte con UNA pregunta cerrada corta que empiece con '{verbo}', "
        f"enfoque SOLO esta escasez: {palanca}, y termine con '? {emoji}'. "
        "Prohibido copiar cierres de mensajes anteriores.]"
    )


def detectar_paso_gratis(texto_recibido, media_recibida, memoria):
    """
    Devuelve (numero_paso, respuesta_fija) SOLO cuando es 100% seguro responder sin IA.
    Ante cualquier pregunta, duda, objeción o adjunto devuelve (None, None) para pasar a Gemini.
    """
    if media_recibida:
        return None, None

    texto_raw = (texto_recibido or "").strip()
    if not texto_raw or "?" in texto_raw or "¿" in texto_raw:
        return None, None

    texto_norm = normalizar_texto(texto_raw)
    palabras = re.findall(r"[a-z0-9]+", texto_norm)
    if not palabras:
        return None, None

    set_palabras = set(palabras)

    # --- PASO 1 GRATIS: Cliente 100% nuevo con mensaje del anuncio o saludo genérico ---
    if not memoria:
        if len(palabras) > 28 or len(texto_norm) > 220:
            return None, None
        if (set_palabras & BLOQUEO_PASO_1_PALABRAS) or any(b in texto_norm for b in BLOQUEO_PASO_1_FRASES):
            return None, None
        if len(palabras) <= 3 or any(s in texto_norm for s in SALUDOS_PASO_1):
            return 1, TEXTO_PASO_1
        return None, None

    # --- PASOS 2, 3 Y 4 GRATIS: Solo confirmaciones cortas y limpias al paso inmediato anterior ---
    if len(palabras) > 7:
        return None, None

    if set_palabras & PALABRAS_BLOQUEO_CONFIRMACION:
        return None, None

    ultima_carla = ""
    for item in reversed(memoria):
        if item.startswith("Tú:"):
            ultima_carla = normalizar_texto(item)
            break

    if not ultima_carla:
        return None, None

    historial_unido = normalizar_texto("\n".join(memoria))
    es_afirmativa = bool(set_palabras & PALABRAS_AFIRMATIVAS)

    # Paso 1 -> Paso 2
    if "crear tu propia marca" in ultima_carla and "negocio rentable" in ultima_carla:
        if es_afirmativa and not (set_palabras & {"precio", "costo", "valor", "bonos", "bono", "plataforma", "certificado", "diploma", "temario"}):
            return 2, TEXTO_PASO_2
        return None, None

    # Paso 2 -> Paso 3
    if "temario oficial" in ultima_carla and "modulos al detalle" in ultima_carla:
        if "temario.pdf" in historial_unido or "certificado.png" in historial_unido:
            return None, None
        if (es_afirmativa or "temario" in set_palabras) and not (set_palabras & {"precio", "costo", "valor", "bonos", "bono", "plataforma"}):
            return 3, TEXTO_PASO_3
        return None, None

    # Paso 3 -> Paso 4
    if "precio promocional" in ultima_carla and "18 bonos" in ultima_carla and "plataforma por dentro" in ultima_carla:
        if "bonos.pdf" in historial_unido or "video-plataforma.mp4" in historial_unido:
            return None, None
        if es_afirmativa or bool(set_palabras & {"precio", "oferta", "bonos", "promocion"}):
            return 4, TEXTO_PASO_4
        return None, None

    return None, None


def seleccionar_anexos(numero_remitente, texto_recibido, media_recibida, secciones):
    """Inyecta dinámicamente solo los anexos relevantes para este turno (pueden combinarse varios)."""
    texto_norm = normalizar_texto(texto_recibido)
    set_palabras = set(re.findall(r"[a-z0-9]+", texto_norm))

    with semaforo:
        est = estado_remarketing.get(numero_remitente, {})
        link_enviado = bool(est.get("link_enviado"))
        pago_confirmado = bool(est.get("pago_confirmado"))

    usar_temario = any(k in texto_norm for k in DISPARADORES_TEMARIO)
    usar_pagos = (
        bool(media_recibida)
        or (link_enviado and not pago_confirmado)
        or bool(set_palabras & DISPARADORES_PAGOS_PALABRAS)
        or any(k in texto_norm for k in DISPARADORES_PAGOS)
    )
    usar_postpago = (
        bool(media_recibida)
        or pago_confirmado
        or bool(set_palabras & DISPARADORES_POSTPAGO_PALABRAS)
        or bool(re.search(r"\bhp\d+", texto_norm))
        or any(k in texto_norm for k in DISPARADORES_POSTPAGO)
    )

    bloques = []
    nombres = []

    if usar_temario and secciones.get("temario"):
        bloques.append(secciones["temario"])
        nombres.append("temario")

    if usar_pagos and secciones.get("pagos"):
        bloques.append(secciones["pagos"])
        nombres.append("pagos")

    if usar_postpago and secciones.get("postpago"):
        bloques.append(secciones["postpago"])
        nombres.append("postpago")

    return "\n\n".join(bloques), nombres


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
    """Cancela cualquier remarketing pendiente y actualiza si el cliente indica que ya pagó o que aún no paga."""
    texto_norm = normalizar_texto(texto_cliente)
    senales_no_pago_cliente = (
        "no he pag",
        "no pague",
        "no lo he pag",
        "todavia no pag",
        "todavia no he pag",
        "aun no pag",
        "aun no he pag",
        "no hice el pago",
        "no realice el pago",
        "sin pagar",
    )
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
        if any(s in texto_norm for s in senales_no_pago_cliente):
            est["pago_confirmado"] = False
        elif any(s in texto_norm for s in senales_pago_cliente):
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


def llamar_gemini_api(instruccion_sistema, prompt_usuario, partes_adjuntas=None, temperatura=0.4):
    """Llama a Gemini 3.5 Flash-Lite usando systemInstruction (con respaldo defensivo si el endpoint lo rechaza)."""
    url_gemini = f"https://generativelanguage.googleapis.com/v1/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    headers_gemini = {"Content-Type": "application/json"}
    parts_payload = [{"text": prompt_usuario}] + (partes_adjuntas or [])

    payload = {
        "systemInstruction": {"parts": [{"text": instruccion_sistema}]},
        "contents": [{"role": "user", "parts": parts_payload}],
        "generationConfig": {"temperature": temperatura},
    }

    res = None
    for intento in range(2):
        try:
            res = requests.post(url_gemini, headers=headers_gemini, json=payload, timeout=(10, 30))
            if res.status_code == 200:
                return res
            log(f"[GEMINI ERROR] Intento {intento + 1} HTTP {res.status_code}: {res.text[:250]}")
            # Respaldo defensivo si un endpoint rechazara systemInstruction
            if res.status_code == 400 and "system" in res.text.lower() and intento == 0:
                log("[GEMINI] Reintentando con prompt combinado por compatibilidad de systemInstruction.")
                payload = {
                    "contents": [{"parts": [{"text": f"{instruccion_sistema}\n\n{prompt_usuario}"}] + (partes_adjuntas or [])}],
                    "generationConfig": {"temperature": temperatura},
                }
                continue
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
            "systemInstruction": {"parts": [{"text": instruccion_sistema}]},
            "contents": [{"role": "user", "parts": [{"text": prompt_usuario}]}],
            "generationConfig": {"temperature": temperatura},
        }
        try:
            res = requests.post(url_gemini, headers=headers_gemini, json=payload_fallback, timeout=(10, 25))
        except requests.exceptions.RequestException as e:
            log(f"[GEMINI FALLBACK EXCEPCION] {e}")

    return res


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

        secciones = cargar_prompt_modular()
        instruccion_base = secciones["nucleo"]
        texto_historial = "Últimos mensajes de contexto:\n" + "\n".join(memoria) if memoria else ""

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

        prompt_rmk = f"{texto_historial}{orden_interna}".strip()
        res = llamar_gemini_api(instruccion_base, prompt_rmk, temperatura=0.25)

        if res is None or res.status_code != 200:
            return

        data = res.json()
        usage = data.get("usageMetadata") or {}
        log(
            f"[TOKENS RMK {numero_remitente}] entrada={usage.get('promptTokenCount', 0)} | "
            f"cache={usage.get('cachedContentTokenCount', 0)} | "
            f"salida={usage.get('candidatesTokenCount', 0)}"
        )

        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content", {}).get("parts") or []) if candidates else []
        texto_rmk = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()

        # Limpieza defensiva: el remarketing de 2 horas es siempre solo texto
        texto_rmk = re.sub(r'\[(VIDEO|IMAGE|DOCUMENT|AUDIO):[^\]]+\]', '', texto_rmk, flags=re.IGNORECASE).strip()
        texto_rmk = limpiar_formato_whatsapp(texto_rmk)
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
                memoria_actual.append(f"Tú: {compactar_para_historial(texto_rmk)}")
                historial_conversaciones[numero_remitente] = memoria_actual[-20:]

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


def enviar_respuesta_carla(numero_remitente, respuesta_final, activar_remarketing=True):
    """Envía texto y/o multimedia en orden a WhatsApp, registra en el panel y actualiza remarketing."""
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
        if activar_remarketing:
            actualizar_y_programar_remarketing(numero_remitente, respuesta_final)
    except Exception:
        log(f"[ENVIO ERROR GENERAL] {traceback.format_exc()}")


# --- CEREBRO DE CARLA (PASOS 1-4 GRATIS + NÚCLEO FIJO + 3 ANEXOS DESDE 1 SOLO TXT + VISIÓN) ---
def procesar_mensaje_gemini(numero_remitente, texto_recibido, media_recibida=None):
    global historial_conversaciones

    if esta_bot_pausado(numero_remitente):
        return

    with semaforo:
        memoria = list(historial_conversaciones.get(numero_remitente, []))

    # --- 1. ATAJO GRATIS BLINDADO (PASOS 1 AL 4 SIN CONSUMIR TOKENS DE IA) ---
    paso_gratis, respuesta_gratis = detectar_paso_gratis(texto_recibido, media_recibida, memoria)
    if paso_gratis and respuesta_gratis:
        with semaforo:
            cli = clientes_panel.get(numero_remitente)
            if cli and cli.get("estado_bot") == "PAUSADO":
                return
            memoria_actual = list(historial_conversaciones.get(numero_remitente, []))
            memoria_actual.append(f"Cliente: {texto_recibido}")
            memoria_actual.append(f"Tú: {compactar_para_historial(respuesta_gratis)}")
            historial_conversaciones[numero_remitente] = memoria_actual[-20:]

        log(f"[PASO {paso_gratis} GRATIS] {numero_remitente} | tokens=0")
        enviar_respuesta_carla(numero_remitente, respuesta_gratis, activar_remarketing=True)
        return

    # --- 2. CEREBRO GEMINI 3.5 FLASH-LITE (NÚCLEO EN systemInstruction + ANEXOS BAJO DEMANDA) ---
    respuesta_final = "Lo siento, tengo un problema técnico temporal."
    gemini_ok = False

    if GEMINI_API_KEY:
        try:
            secciones = cargar_prompt_modular()
            instruccion_base = secciones["nucleo"]
            bloque_anexos, anexos_usados = seleccionar_anexos(
                numero_remitente, texto_recibido, media_recibida, secciones
            )

            texto_historial = ""
            if memoria:
                texto_historial = "Últimos mensajes de contexto:\n" + "\n".join(memoria)

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
                        "[INSTRUCCIÓN DE VISIÓN: El prospecto adjuntó una imagen o documento PDF. "
                        "Obsérvalo con atención (puede ser un comprobante de Yape/Tarjeta, un voucher CIP pendiente o pagado de PagoEfectivo, "
                        "una captura del checkout, del correo de Hotmart o un error en pantalla) y responde exactamente según lo que ves y tus reglas.]"
                    )

            nota_caso_d = obtener_nota_rotacion_caso_d(numero_remitente, memoria, media_recibida)

            secciones_usuario = []
            if bloque_anexos:
                secciones_usuario.append(bloque_anexos)
            if texto_historial:
                secciones_usuario.append(texto_historial)
            if nota_vision:
                secciones_usuario.append(nota_vision)
            if nota_caso_d:
                secciones_usuario.append(nota_caso_d)
            secciones_usuario.append(f"Mensaje actual del prospecto: '{texto_recibido}'")
            prompt_usuario = "\n\n".join(secciones_usuario)

            res = llamar_gemini_api(instruccion_base, prompt_usuario, partes_adjuntas=partes_adjuntas, temperatura=0.4)

            if res is not None and res.status_code == 200:
                data = res.json()
                usage = data.get("usageMetadata") or {}
                log(
                    f"[TOKENS {numero_remitente}] anexos={anexos_usados or ['ninguno']} | "
                    f"entrada={usage.get('promptTokenCount', 0)} | "
                    f"cache={usage.get('cachedContentTokenCount', 0)} | "
                    f"salida={usage.get('candidatesTokenCount', 0)}"
                )

                candidates = data.get("candidates") or []
                parts = (candidates[0].get("content", {}).get("parts") or []) if candidates else []
                texto_generado = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()

                if texto_generado:
                    respuesta_final = limpiar_formato_whatsapp(normalizar_etiquetas_multimedia(texto_generado))
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
                    memoria_actual.append(f"Tú: {compactar_para_historial(respuesta_final)}")
                    historial_conversaciones[numero_remitente] = memoria_actual[-20:]
        except Exception:
            log(f"[GEMINI ERROR GENERAL] {traceback.format_exc()}")
    else:
        log("[GEMINI ERROR] GEMINI_API_KEY no configurada.")

    enviar_respuesta_carla(numero_remitente, respuesta_final, activar_remarketing=gemini_ok)


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
    return "Servidor activo, conectado a WhatsApp y Gemini 3.5 Flash-Lite (Arquitectura Modular).", 200


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
        historial_conversaciones[telefono] = memoria_actual[-20:]

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
