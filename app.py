import os
import requests
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "bot_melamina_cejas_2026")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")

# LLAVE DE GOOGLE PROTEGIDA (Se lee desde Render, no desde GitHub)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

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
        
        # ESTA ES LA LUPA: Imprimimos el error exacto de Meta antes de que lance la excepción
        if respuesta.status_code != 200:
            print("\n" + "="*50)
            print(f"🛑 RECHAZO EXACTO DE META: {respuesta.text}")
            print("="*50 + "\n")
            
        respuesta.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error enviando mensaje a Meta: {e}")
        return False

def procesar_mensaje_gemini(numero_remitente, texto_recibido):
    respuesta_final = "Lo siento, tengo un problema técnico temporal."
    
    if GEMINI_API_KEY:
        try:
            # CORRECCIÓN FINAL: Usamos el modelo gemini-3.8-flash que el propio Google exigió en el error
            url_gemini = f"https://generativelanguage.googleapis.com/v1/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
            headers_gemini = {'Content-Type': 'application/json'}
            
            # --- INICIO DEL CEREBRO EXTERNO ---
            try:
                # Leemos el archivo prompt_ventas.txt en cada petición
                with open("prompt_ventas.txt", "r", encoding="utf-8") as archivo:
                    instruccion_base = archivo.read().strip()
            except Exception as e:
                print(f"Error leyendo el prompt: {e}")
                instruccion_base = "Eres un asistente de ventas."
            
            # Unimos la inteligencia comercial externa con el mensaje del prospecto
            prompt = f"{instruccion_base}\n\nMensaje del prospecto: '{texto_recibido}'"
            # --- FIN DEL CEREBRO EXTERNO ---
            
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
