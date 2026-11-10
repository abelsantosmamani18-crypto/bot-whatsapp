import os
import requests
import threading
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "bot_melamina_cejas_2026")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")

# LLAVE DE GOOGLE PROTEGIDA
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- CANDADO 2: MEMORIA RAM GRATUITA (Diccionario temporal) ---
historial_conversaciones = {}

@app.route("/", methods=["GET"])
def home():
    return "Servidor activo, conectado a WhatsApp y Gemini 3.8 Flash.", 200

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
def enviar_multimedia_whatsapp(telefono_destino, tipo_media, url_archivo, nombre_archivo=""):
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

    # WhatsApp exige un "filename" (nombre) cuando se envían documentos PDF por URL
    if tipo_lower == "document" and nombre_archivo:
        payload[tipo_lower]["filename"] = nombre_archivo
    
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
            
            # 1. Leer el cerebro comercial (el prompt)
            try:
                with open("prompt_ventas.txt", "r", encoding="utf-8") as archivo:
                    instruccion_base = archivo.read().strip()
            except Exception as e:
                print(f"Error leyendo el prompt: {e}")
                instruccion_base = "Eres un asistente de ventas."
            
            # --- CANDADO 2: RECUPERAR LOS ÚLTIMOS 4 MENSAJES ---
            memoria = historial_conversaciones.get(numero_remitente, [])
            texto_historial = ""
            if memoria:
                texto_historial = "\n\nÚltimos mensajes de contexto:\n" + "\n".join(memoria)
            
            # Unimos la inteligencia + la memoria reciente + el nuevo mensaje
            prompt = f"{instruccion_base}{texto_historial}\n\nMensaje actual del prospecto: '{texto_recibido}'"
            
            payload_gemini = {
                "contents": [{"parts": [{"text": prompt}]}]
            }
            
            res = requests.post(url_gemini, headers=headers_gemini, json=payload_gemini)
            
            if res.status_code == 200:
                data = res.json()
                respuesta_final = data['candidates'][0]['content']['parts'][0]['text']
                
                # --- CANDADO 2: GUARDAR HISTORIA Y RECORTAR ---
                memoria.append(f"Cliente: {texto_recibido}")
                memoria.append(f"Tú: {respuesta_final}")
                historial_conversaciones[numero_remitente] = memoria[-4:]
                
            else:
                print(f"Error real de Gemini: {res.text}")
        except Exception as e:
            print(f"Error en la conexión directa a Gemini: {e}")
    else:
        print("ADVERTENCIA: No hay llave de Gemini configurada.")

    # --- INTERCEPTOR MULTIMEDIA POR URL ---
    patron_multimedia = r'\[(VIDEO|IMAGE|DOCUMENT|AUDIO):([^\]]+)\]'
    etiquetas_encontradas = re.findall(patron_multimedia, respuesta_final, re.IGNORECASE)
    
    # 1. Enviar primero los archivos detectados extrayendo la URL directa
    for tipo_media, url_archivo in etiquetas_encontradas:
        nombre_archivo = url_archivo.split("/")[-1] # Extrae el nombre final (ej. temario.pdf)
        enviar_multimedia_whatsapp(numero_remitente, tipo_media, url_archivo, nombre_archivo)
        
    # 2. Limpiar la etiqueta del texto para que el cliente no la vea
    texto_limpio = re.sub(patron_multimedia, '', respuesta_final, flags=re.IGNORECASE).strip()
    
    # 3. Enviar el texto final
    if texto_limpio:
        enviar_mensaje_whatsapp(numero_remitente, texto_limpio)

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
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
                        
                        # --- CANDADO 1: FILTRO DE EVENTOS BASURA ---
                        if "statuses" in value:
                            continue 
                        
                        if "messages" in value:
                            mensaje = value["messages"][0]
                            numero_remitente = mensaje["from"]
                            
                            if mensaje["type"] == "text":
                                texto_recibido = mensaje["text"]["body"]
                                print(f"Mensaje de {numero_remitente}: {texto_recibido}")
                                
                                hilo = threading.Thread(target=procesar_mensaje_gemini, args=(numero_remitente, texto_recibido))
                                hilo.start()

        except Exception as e:
            print(f"Error general en el webhook: {e}")

        return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
