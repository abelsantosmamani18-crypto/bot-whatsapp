import os
import requests
import threading
import re
import time
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "bot_melamina_cejas_2026")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- MEMORIA RAM Y BUZÓN TEMPORAL ---
historial_conversaciones = {}
buzon_mensajes = {}
timers = {}
candados_clientes = {}
mensajes_procesados = set()
ESPERA_SEGUNDOS = 4.0

# --- EL CANDADO DE SEGURIDAD ---
semaforo = threading.Lock()

@app.route("/", methods=["GET"])
def home():
    return "Servidor activo, conectado a WhatsApp y Gemini 3.5 Flash-Lite.", 200

def enviar_mensaje_whatsapp(telefono_destino, texto_mensaje):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        return False
        
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono_destino,
        "type": "text",
        "text": {"body": texto_mensaje}
    }
    
    try:
        respuesta = requests.post(url, headers=headers, json=payload)
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
        "Content-Type": "application/json"
    }
    
    tipo_lower = tipo_media.lower() 
    
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono_destino,
        "type": tipo_lower,
        tipo_lower: {"link": url_archivo}
    }

    if tipo_lower == "document" and nombre_archivo:
        payload[tipo_lower]["filename"] = nombre_archivo
        
    if caption and tipo_lower in ["video", "image", "document"]:
        payload[tipo_lower]["caption"] = caption
    
    try:
        respuesta = requests.post(url, headers=headers, json=payload)
        respuesta.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error enviando multimedia a Meta: {e}")
        return False

def procesar_mensaje_gemini(numero_remitente, texto_recibido):
    global historial_conversaciones
    respuesta_final = "Lo siento, tengo un problema técnico temporal."
    
    if GEMINI_API_KEY:
        try:
            url_gemini = f"https://generativelanguage.googleapis.com/v1/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
            headers_gemini = {'Content-Type': 'application/json'}
            
            try:
                with open("prompt_ventas.txt", "r", encoding="utf-8") as archivo:
                    instruccion_base = archivo.read().strip()
            except Exception as e:
                instruccion_base = "Eres un asistente de ventas."
            
            with semaforo:
                memoria = list(historial_conversaciones.get(numero_remitente, []))
                
            texto_historial = ""
            if memoria:
                texto_historial = "\n\nÚltimos mensajes de contexto:\n" + "\n".join(memoria)
            
            prompt = f"{instruccion_base}{texto_historial}\n\nMensaje actual del prospecto: '{texto_recibido}'"
            
            payload_gemini = {
                "contents": [{"parts": [{"text": prompt}]}]
            }
            
            res = requests.post(url_gemini, headers=headers_gemini, json=payload_gemini)
            
            if res.status_code == 200:
                data = res.json()
                respuesta_final = data['candidates'][0]['content']['parts'][0]['text']
                
                # --- INTERCEPTOR ANTI-DOBLE RESPUESTA ---
                with semaforo:
                    if numero_remitente in buzon_mensajes and buzon_mensajes[numero_remitente]:
                        buzon_mensajes[numero_remitente].insert(0, texto_recibido)
                        return
                        
                    memoria_actual = list(historial_conversaciones.get(numero_remitente, []))
                    memoria_actual.append(f"Cliente: {texto_recibido}")
                    memoria_actual.append(f"Tú: {respuesta_final}")
                    historial_conversaciones[numero_remitente] = memoria_actual[-10:]
        except Exception as e:
            print(f"Error en la conexión a Gemini: {e}")

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
                caption=caption_actual
            )
            
            if not exito and caption_actual:
                enviar_mensaje_whatsapp(numero_remitente, caption_actual)
                
            if not es_ultimo:
                time.sleep(4.0)
    else:
        texto_limpio = respuesta_final.strip()
        if texto_limpio:
            enviar_mensaje_whatsapp(numero_remitente, texto_limpio)

def despachar_buzon(numero_remitente):
    global buzon_mensajes, timers, candados_clientes
    
    with semaforo:
        if numero_remitente not in candados_clientes:
            candados_clientes[numero_remitente] = threading.Lock()
        candado_cliente = candados_clientes[numero_remitente]

    with candado_cliente:
        with semaforo:
            if numero_remitente in buzon_mensajes and buzon_mensajes[numero_remitente]:
                textos_agrupados = " ".join(buzon_mensajes[numero_remitente])
                
                del buzon_mensajes[numero_remitente]
                if numero_remitente in timers:
                    del timers[numero_remitente]
            else:
                return

        procesar_mensaje_gemini(numero_remitente, textos_agrupados)

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    global buzon_mensajes, timers, mensajes_procesados
    
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
                            
                            if mensaje["type"] == "text":
                                texto_recibido = mensaje["text"]["body"]
                                
                                with semaforo:
                                    if id_mensaje:
                                        if id_mensaje in mensajes_procesados:
                                            continue
                                        mensajes_procesados.add(id_mensaje)
                                        if len(mensajes_procesados) > 1000:
                                            mensajes_procesados.clear()

                                    if numero_remitente not in buzon_mensajes:
                                        buzon_mensajes[numero_remitente] = []
                                    
                                    buzon_mensajes[numero_remitente].append(texto_recibido)
                                    
                                    if numero_remitente in timers:
                                        timers[numero_remitente].cancel()
                                    
                                    timers[numero_remitente] = threading.Timer(ESPERA_SEGUNDOS, despachar_buzon, args=[numero_remitente])
                                    timers[numero_remitente].start()

        except Exception as e:
            print(f"Error general en el webhook: {e}")

        return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
