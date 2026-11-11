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

# LLAVE DE GOOGLE PROTEGIDA
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- CANDADO 2: MEMORIA RAM GRATUITA (Diccionario temporal) ---
historial_conversaciones = {}

# --- NUEVO: BUZÓN TEMPORAL PARA AGRUPAR MENSAJES ---
buzon_mensajes = {}
timers = {}
ESPERA_SEGUNDOS = 4.0

@app.route("/", methods=["GET"])
def home():
    return "Servidor activo, conectado a WhatsApp y Gemini 3.8 Flash.", 200

# --- NUEVO: FUNCIÓN PARA MOSTRAR "ESCRIBIENDO..." ---
def enviar_estado_escribiendo(telefono_destino):
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
        "type": "contacts", # Se requiere este campo aunque enviemos estado
        "text": {"body": ""}, # Se requiere un body vacío
        # La forma correcta en la API v21 para el estado
        "action": "typing_on"
    }
    
    # Meta requiere que sea un payload especial para mark_as_read o typing_on
    payload_typing = {
        "messaging_product": "whatsapp",
        "to": telefono_destino,
        "type": "text", 
        "text": {"body": ""} # Placeholder, Meta lo ignora si pasamos el sender_action abajo
    }
    
    # Payload oficial según la documentación de Cloud API
    payload_oficial = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": telefono_destino,
        "type": "text",
        "text": {"body": ""}
    }
    
    # La API actual a veces prefiere que se haga a través de un campo "action" no documentado, 
    # pero como es experimental, simplemente enviarlo no rompe la app. 
    # Si la API v21 estricta lo rechaza silenciosamente, al menos no detiene el bot.
    pass # Por ahora lo omitiremos si no tienes una plantilla oficial de "typing_on" activa, para evitar errores 400.

# Usaremos una versión simplificada que Meta acepta para marcar mensajes leídos si es necesario, 
# pero el "typing_on" no está nativamente soportado en todos los tiers de la API Cloud gratuita.
# Lo dejaré preparado pero inactivo para que no te dé errores 400 de Meta, y me enfocaré en el temporizador.


def enviar_mensaje_whatsapp(telefono_destino, texto_mensaje):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("ERROR: Faltan credenciales de WhatsApp.")
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
        "text": {
            "body": texto_mensaje
        }
    }
    
    try:
        respuesta = requests.post(url, headers=headers, json=payload)
        if respuesta.status_code != 200:
            print("\n" + "="*50)
            print(f"🛑 RECHAZO EXACTO DE META (TEXTO): {respuesta.text}")
            print("="*50 + "\n")
            
        respuesta.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error enviando mensaje a Meta: {e}")
        return False

# --- CANDADO 3: MOTOR MULTIMEDIA UNIVERSAL (DESACOPLADO POR URL) ---
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
        tipo_lower: {
            "link": url_archivo
        }
    }

    if tipo_lower == "document" and nombre_archivo:
        payload[tipo_lower]["filename"] = nombre_archivo
        
    if caption and tipo_lower in ["video", "image", "document"]:
        payload[tipo_lower]["caption"] = caption
    
    try:
        respuesta = requests.post(url, headers=headers, json=payload)
        if respuesta.status_code != 200:
            print("\n" + "="*50)
            print(f"🛑 RECHAZO EXACTO DE META (MULTIMEDIA): {respuesta.text}")
            print("="*50 + "\n")
            
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
            url_gemini = f"https://generativelanguage.googleapis.com/v1/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
            headers_gemini = {'Content-Type': 'application/json'}
            
            try:
                with open("prompt_ventas.txt", "r", encoding="utf-8") as archivo:
                    instruccion_base = archivo.read().strip()
            except Exception as e:
                print(f"Error leyendo el prompt: {e}")
                instruccion_base = "Eres un asistente de ventas."
            
            memoria = historial_conversaciones.get(numero_remitente, [])
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
                
                memoria.append(f"Cliente: {texto_recibido}")
                memoria.append(f"Tú: {respuesta_final}")
                historial_conversaciones[numero_remitente] = memoria[-10:]
                
            else:
                print(f"Error real de Gemini: {res.text}")
        except Exception as e:
            print(f"Error en la conexión directa a Gemini: {e}")
    else:
        print("ADVERTENCIA: No hay llave de Gemini configurada.")

    patron_multimedia = r'\[(VIDEO|IMAGE|DOCUMENT|AUDIO):([^\]]+)\]'
    etiquetas_encontradas = re.findall(patron_multimedia, respuesta_final, re.IGNORECASE)
    
    texto_limpio = re.sub(patron_multimedia, '', respuesta_final, flags=re.IGNORECASE).strip()
    
    if etiquetas_encontradas:
        total_archivos = len(etiquetas_encontradas)
        for i, (tipo_media, url_archivo) in enumerate(etiquetas_encontradas):
            url_archivo = url_archivo.strip()
            nombre_archivo = url_archivo.split("/")[-1]
            
            es_ultimo = (i == total_archivos - 1)
            caption_actual = texto_limpio if es_ultimo else ""
            
            exito = enviar_multimedia_whatsapp(numero_remitente, tipo_media, url_archivo, nombre_archivo, caption=caption_actual)
            
            if es_ultimo and not exito and texto_limpio:
                enviar_mensaje_whatsapp(numero_remitente, texto_limpio)
    else:
        if texto_limpio:
            enviar_mensaje_whatsapp(numero_remitente, texto_limpio)

# --- NUEVO: FUNCIÓN PARA DESPACHAR EL BUZÓN ACUMULADO ---
def despachar_buzon(numero_remitente):
    global buzon_mensajes, timers
    
    if numero_remitente in buzon_mensajes:
        # Extraemos todos los mensajes que el cliente envió en esos 4 segundos
        textos_agrupados = " ".join(buzon_mensajes[numero_remitente])
        print(f"Procesando bloque agrupado de {numero_remitente}: {textos_agrupados}")
        
        # Limpiamos el buzón y el timer de este cliente
        del buzon_mensajes[numero_remitente]
        del timers[numero_remitente]
        
        # Le enviamos TODO JUNTO a la IA
        procesar_mensaje_gemini(numero_remitente, textos_agrupados)


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    global buzon_mensajes, timers
    
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
                            numero_remitente = mensaje["from"]
                            
                            if mensaje["type"] == "text":
                                texto_recibido = mensaje["text"]["body"]
                                print(f"Mensaje entrante de {numero_remitente}: {texto_recibido}")
                                
                                # --- NUEVA LÓGICA DE AGRUPACIÓN (4 SEGUNDOS) ---
                                
                                # 1. Si no existe un buzón para este cliente, lo creamos
                                if numero_remitente not in buzon_mensajes:
                                    buzon_mensajes[numero_remitente] = []
                                
                                # 2. Agregamos el texto nuevo al buzón
                                buzon_mensajes[numero_remitente].append(texto_recibido)
                                
                                # 3. Si ya había un timer corriendo, lo cancelamos
                                if numero_remitente in timers:
                                    timers[numero_remitente].cancel()
                                
                                # 4. Iniciamos un NUEVO timer de 4 segundos
                                timers[numero_remitente] = threading.Timer(ESPERA_SEGUNDOS, despachar_buzon, args=[numero_remitente])
                                timers[numero_remitente].start()
                                
                                # NOTA: La función procesar_mensaje_gemini YA NO se llama inmediatamente.
                                # Ahora espera 4 segundos de inactividad del usuario antes de ejecutarse.

        except Exception as e:
            print(f"Error general en el webhook: {e}")

        return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
