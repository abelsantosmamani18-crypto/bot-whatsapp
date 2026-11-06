import os
import requests
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "bot_melamina_cejas_2026")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")

# LLAVE DE GOOGLE - Confirmado que está activa y sin caducar
GEMINI_API_KEY = "AQ.Ab8RN6J_pcegUE9MFX8VCtQvslmcA4XgfC9YkAx701Vd4W4AWA"

@app.route("/", methods=["GET"])
def home():
    return "Servidor activo, conectado a WhatsApp y Gemini (v1).", 200

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
        respuesta.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error enviando mensaje a Meta: {e}")
        return False

def procesar_mensaje_gemini(numero_remitente, texto_recibido):
    respuesta_final = "Lo siento, tengo un problema técnico temporal."
    
    if GEMINI_API_KEY:
        try:
            # CORRECCIÓN DEFINITIVA: Usamos la ruta oficial v1 y el modelo actual gemini-2.0-flash
            url_gemini = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
            headers_gemini = {'Content-Type': 'application/json'}
            prompt = f"Eres el cerrador de ventas de 'Crea con Melamina'. Responde de forma amable, persuasiva y corta a este mensaje de un prospecto: '{texto_recibido}'. Regla obligatoria: Genera tu respuesta en texto completamente limpio, sin usar simbolos, sin negritas, sin asteriscos, sin viñetas y sin caracteres especiales."
            
            payload_gemini = {
                "contents": [{"parts": [{"text": prompt}]}]
            }
            
            res = requests.post(url_gemini, headers=headers_gemini, json=payload_gemini)
            
            if res.status_code == 200:
                data = res.json()
                respuesta_final = data['candidates'][0]['content']['parts'][0]['text']
            else:
                print(f"Error real de Gemini: {res.text}")
        except Exception as e:
            print(f"Error en la conexión directa a Gemini: {e}")
    else:
        print("ADVERTENCIA: No hay llave de Gemini configurada.")

    enviar_mensaje_whatsapp(numero_remitente, respuesta_final)

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
