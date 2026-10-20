import os
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "bot_melamina_cejas_2026")

@app.route("/", methods=["GET"])
def home():
    return "Servidor del Bot activo y funcionando.", 200

# AQUI ESTA LA MAGIA: Agregamos POST para recibir mensajes
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
        # Extraemos la información del mensaje que manda Meta
        body = request.get_json()
        print("====== NUEVO MENSAJE RECIBIDO ======")
        print(body)
        print("====================================")

        # RESPUESTA OBLIGATORIA A META PARA QUE NO TE BLOQUEE
        return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
