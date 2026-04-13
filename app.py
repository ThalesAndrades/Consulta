"""
Sistema Consulta Médica Online — R$99,90
Flask + SQLite + Asaas PIX Sandbox + Rapidoc Telemedicina
"""
import os, uuid, json as _json, smtplib, requests, logging, time
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import (Flask, request, jsonify, render_template,
                   redirect, url_for, session)
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-troque-em-producao")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///consulta_medica.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# MODELOS
# ─────────────────────────────────────────────────────────────────────────────
class Paciente(db.Model):
    __tablename__ = "pacientes"
    id              = db.Column(db.Integer,     primary_key=True)
    nome            = db.Column(db.String(200), nullable=False)
    cpf             = db.Column(db.String(14),  nullable=False)
    email           = db.Column(db.String(200), nullable=False)
    telefone        = db.Column(db.String(20),  nullable=False)
    data_nascimento = db.Column(db.Date,        nullable=False)
    estado          = db.Column(db.String(2),   nullable=False)
    cidade          = db.Column(db.String(100), nullable=False)
    cep             = db.Column(db.String(10),  nullable=True)
    endereco        = db.Column(db.String(300), nullable=False)
    rapidoc_uuid    = db.Column(db.String(100), nullable=True)
    rapidoc_raw     = db.Column(db.Text,        nullable=True)
    criado_em       = db.Column(db.DateTime,    default=datetime.utcnow)
    pagamentos      = db.relationship("Pagamento", back_populates="paciente")


class Pagamento(db.Model):
    __tablename__ = "pagamentos"
    id              = db.Column(db.Integer,   primary_key=True)
    paciente_id     = db.Column(db.Integer,   db.ForeignKey("pacientes.id"), nullable=False)
    status          = db.Column(db.String(20),default="pendente")
    asaas_id        = db.Column(db.String(100),nullable=True)
    asaas_customer  = db.Column(db.String(100),nullable=True)
    valor           = db.Column(db.Float,     default=99.90)
    qr_code_img     = db.Column(db.Text,      nullable=True)
    qr_code_payload = db.Column(db.Text,      nullable=True)
    link_consulta   = db.Column(db.Text,      nullable=True)
    criado_em       = db.Column(db.DateTime,  default=datetime.utcnow)
    aprovado_em     = db.Column(db.DateTime,  nullable=True)
    paciente        = db.relationship("Paciente", back_populates="pagamentos")


# ─────────────────────────────────────────────────────────────────────────────
# ASAAS
# ─────────────────────────────────────────────────────────────────────────────
ASAAS_SANDBOX = os.getenv("ASAAS_SANDBOX", "true").lower() == "true"
ASAAS_BASE    = ("https://sandbox.asaas.com/api/v3" if ASAAS_SANDBOX
                 else "https://api.asaas.com/v3")
ASAAS_KEY     = os.getenv("ASAAS_API_KEY", "")

def _ah():
    return {"Content-Type": "application/json", "access_token": ASAAS_KEY}

def asaas_criar_cliente(p) -> str:
    cpf = p.cpf.replace(".", "").replace("-", "")
    tel = p.telefone.replace("(","").replace(")","").replace(" ","").replace("-","")
    r = requests.post(f"{ASAAS_BASE}/customers",
        json={"name": p.nome, "cpfCnpj": cpf, "email": p.email, "phone": tel},
        headers=_ah(), timeout=15)
    log.info("Asaas criar cliente %s %s", r.status_code, r.text[:200])
    r.raise_for_status()
    return r.json()["id"]

def asaas_criar_cobranca(cus_id: str, ref: str) -> dict:
    r = requests.post(f"{ASAAS_BASE}/payments",
        json={"customer": cus_id, "billingType": "PIX", "value": 99.90,
              "dueDate": date.today().strftime("%Y-%m-%d"),
              "description": "Consulta médica online",
              "externalReference": ref},
        headers=_ah(), timeout=15)
    log.info("Asaas criar cobrança %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()

def asaas_qrcode(pay_id: str) -> dict:
    """Busca QR Code PIX com retry — Asaas pode demorar 1-2s para processar."""
    for tentativa in range(3):
        time.sleep(1.5 if tentativa == 0 else 2)  # aguarda processamento
        r = requests.get(f"{ASAAS_BASE}/payments/{pay_id}/pixQrCode",
                         headers=_ah(), timeout=15)
        log.info("Asaas QRCode tentativa=%d status=%s body=%s",
                 tentativa+1, r.status_code, r.text[:300])
        if r.status_code == 200:
            data = r.json()
            if data.get("encodedImage") or data.get("payload"):
                return data
        if r.status_code == 404:
            # Ainda processando — tenta novamente
            continue
        # Outros erros: loga e propaga
        r.raise_for_status()
    raise Exception(f"QR Code não disponível após 3 tentativas. Verifique se há uma chave PIX cadastrada em sandbox.asaas.com → Minha Conta → Chaves PIX")

def asaas_status(pay_id: str) -> str:
    r = requests.get(f"{ASAAS_BASE}/payments/{pay_id}",
                     headers=_ah(), timeout=10)
    r.raise_for_status()
    return r.json().get("status", "PENDING")


# ─────────────────────────────────────────────────────────────────────────────
# RAPIDOC
# ─────────────────────────────────────────────────────────────────────────────
def rapidoc_registrar(p) -> dict:
    url   = os.getenv("RAPIDOC_API_URL", "https://sandbox.rapidoc.tech/tema/api/beneficiaries")
    tok   = os.getenv("RAPIDOC_TOKEN", "")
    cid   = os.getenv("RAPIDOC_CLIENT_ID", "")
    payload = [{"name": p.nome,
                "cpf":  p.cpf.replace(".", "").replace("-", ""),
                "birthday": p.data_nascimento.strftime("%d/%m/%Y"),
                "phone": p.telefone.replace("(","").replace(")","").replace(" ","").replace("-",""),
                "email": p.email,
                "zipCode": (p.cep or "").replace("-", ""),
                "address": p.endereco,
                "city":  p.cidade,
                "state": p.estado}]
    r = requests.post(url, json=payload,
        headers={"Content-Type":"application/json",
                 "Authorization": f"Bearer {tok}", "clientId": cid},
        timeout=20)
    log.info("Rapidoc %s %s", r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()

def rapidoc_link(uid: str) -> str:
    base = os.getenv("RAPIDOC_PORTAL_URL", "https://telemedicina.rapidoc.tech/login")
    return f"{base}?uuid={uid}" if uid else base


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────────────────────
def enviar_email(p, link: str):
    u = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")
    if not u or not pw:
        log.warning("SMTP não configurado — email não enviado")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "✅ Sua consulta médica está confirmada!"
        msg["From"] = os.getenv("EMAIL_FROM", u)
        msg["To"]   = p.email
        nome1 = p.nome.split()[0]
        msg.attach(MIMEText(f"""<html><body style="font-family:Georgia,serif;background:#f9f9f9;padding:40px 0">
<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden">
<div style="background:#1a3a4a;padding:32px 40px;text-align:center">
  <h1 style="color:#fff;margin:0;font-size:22px;font-weight:400">CONSULTA CONFIRMADA</h1></div>
<div style="padding:40px">
  <p style="color:#333;font-size:16px">Olá, <strong>{nome1}</strong>.</p>
  <p style="color:#555;font-size:15px;line-height:1.7">Pagamento confirmado! Acesse sua consulta:</p>
  <div style="text-align:center;margin:32px 0">
    <a href="{link}" style="background:#1a3a4a;color:#fff;padding:16px 36px;border-radius:8px;text-decoration:none;font-size:16px">Acessar Minha Consulta</a></div>
  <p style="color:#999;font-size:13px">Link: <a href="{link}">{link}</a></p>
  <p style="color:#aaa;font-size:12px;text-align:center;margin-top:32px">Atendimento sujeito à avaliação médica</p>
</div></div></body></html>""", "html"))
        with smtplib.SMTP(os.getenv("SMTP_HOST","smtp.gmail.com"), int(os.getenv("SMTP_PORT",587))) as s:
            s.starttls(); s.login(u, pw)
            s.sendmail(msg["From"], p.email, msg.as_string())
        log.info("Email enviado → %s", p.email)
    except Exception as e:
        log.error("Erro email: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS INTERNOS
# ─────────────────────────────────────────────────────────────────────────────
def _abs(endpoint, **kw):
    """url_for com _external=True para sempre gerar URL absoluta."""
    return url_for(endpoint, _external=True, **kw)

def _processar_aprovado(pag):
    pag.status = "aprovado"
    pag.aprovado_em = datetime.utcnow()
    if not pag.link_consulta:
        uid = pag.paciente.rapidoc_uuid or ""
        if uid:
            pag.link_consulta = rapidoc_link(uid)
        else:
            try:
                rr = rapidoc_registrar(pag.paciente)
                for b in rr.get("beneficiaries", []):
                    cpf = pag.paciente.cpf.replace(".", "").replace("-", "")
                    if b.get("cpf") == cpf:
                        pag.paciente.rapidoc_uuid = b.get("uuid", "")
                        break
                pag.link_consulta = rapidoc_link(pag.paciente.rapidoc_uuid or "")
            except Exception as e:
                log.error("Rapidoc no aprovado: %s", e)
                pag.link_consulta = "#erro-contate-suporte"
    db.session.commit()
    enviar_email(pag.paciente, pag.link_consulta)


# ─────────────────────────────────────────────────────────────────────────────
# ROTAS
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "GET":
        return render_template("cadastro.html")

    data = request.get_json(silent=True) or request.form.to_dict()

    for campo in ["nome","cpf","email","telefone","data_nascimento","estado","cidade","endereco"]:
        if not data.get(campo, "").strip():
            return jsonify({"erro": f"Campo '{campo}' é obrigatório."}), 400
    try:
        nasc = datetime.strptime(data["data_nascimento"], "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"erro": "Data de nascimento inválida."}), 400

    p = Paciente(
        nome=data["nome"].strip(),
        cpf=data["cpf"].strip(),
        email=data["email"].strip().lower(),
        telefone=data["telefone"].strip(),
        data_nascimento=nasc,
        estado=data["estado"].strip().upper(),
        cidade=data["cidade"].strip(),
        cep=data.get("cep","").strip() or None,
        endereco=data["endereco"].strip(),
    )
    db.session.add(p)
    db.session.flush()

    # Rapidoc
    link = ""
    try:
        rr = rapidoc_registrar(p)
        p.rapidoc_raw = _json.dumps(rr, ensure_ascii=False)
        cpf_num = p.cpf.replace(".", "").replace("-", "")
        for b in rr.get("beneficiaries", []):
            if b.get("cpf") == cpf_num:
                p.rapidoc_uuid = b.get("uuid", "")
                break
        link = rapidoc_link(p.rapidoc_uuid or "")
    except Exception as e:
        log.error("Rapidoc cadastro: %s", e)

    # Asaas PIX
    ref = uuid.uuid4().hex
    pag = Pagamento(paciente_id=p.id, link_consulta=link)
    try:
        cus = asaas_criar_cliente(p)
        pag.asaas_customer = cus
        cob = asaas_criar_cobranca(cus, ref)
        pag.asaas_id = cob["id"]
        qr = asaas_qrcode(cob["id"])
        pag.qr_code_img     = qr.get("encodedImage", "")
        pag.qr_code_payload = qr.get("payload", "")
    except Exception as e:
        log.error("Asaas cadastro: %s", e)
        # Tenta extrair mensagem do Asaas para mostrar ao admin
        erro_msg = str(e)
        if hasattr(e, 'response') and e.response is not None:
            try:
                body = e.response.json()
                erros = body.get("errors", [])
                if erros:
                    erro_msg = erros[0].get("description", erro_msg)
                log.error("Asaas body erro: %s", e.response.text[:500])
            except Exception:
                pass
        pag.asaas_id = f"pay_MOCK_{ref[:8]}"
        pag.qr_code_payload = ""  # vazio = mostra aviso no HTML

    db.session.add(pag)
    db.session.commit()

    session["pagamento_id"] = pag.id
    # ← URL ABSOLUTA: resolve problema de subdiretório
    return jsonify({"redirect": _abs("pagamento", pagamento_id=pag.id)})


@app.route("/pagamento/<int:pagamento_id>")
def pagamento(pagamento_id):
    pag = Pagamento.query.get_or_404(pagamento_id)
    if session.get("pagamento_id") != pagamento_id:
        return redirect(_abs("landing"))
    return render_template("pagamento.html", pagamento=pag,
                           paciente=pag.paciente, sandbox=ASAAS_SANDBOX)


@app.route("/status/<int:pagamento_id>")
def status_pagamento(pagamento_id):
    pag = Pagamento.query.get_or_404(pagamento_id)
    if pag.status == "aprovado":
        return jsonify({"status":"aprovado",
                        "redirect": _abs("sucesso", pagamento_id=pagamento_id)})
    if pag.asaas_id and not pag.asaas_id.startswith("pay_MOCK_"):
        try:
            st = asaas_status(pag.asaas_id)
            if st in ("RECEIVED","CONFIRMED"):
                _processar_aprovado(pag)
                return jsonify({"status":"aprovado",
                                "redirect": _abs("sucesso", pagamento_id=pagamento_id)})
        except Exception as e:
            log.error("Asaas status: %s", e)
    return jsonify({"status": pag.status})


@app.route("/sucesso/<int:pagamento_id>")
def sucesso(pagamento_id):
    pag = Pagamento.query.get_or_404(pagamento_id)
    if pag.status != "aprovado":
        return redirect(_abs("pagamento", pagamento_id=pagamento_id))
    return render_template("sucesso.html", pagamento=pag, paciente=pag.paciente)


@app.route("/webhook/asaas", methods=["POST"])
def webhook_asaas():
    try:
        body = request.get_json()
    except Exception:
        return jsonify({"erro": "Payload inválido"}), 400
    evento = body.get("event","")
    pay_id = body.get("payment",{}).get("id","")
    if evento in ("PAYMENT_RECEIVED","PAYMENT_CONFIRMED") and pay_id:
        pag = Pagamento.query.filter_by(asaas_id=pay_id).first()
        if pag and pag.status != "aprovado":
            _processar_aprovado(pag)
    return jsonify({"ok": True}), 200


@app.route("/dev/simular/<int:pagamento_id>", methods=["POST"])
def dev_simular(pagamento_id):
    if not ASAAS_SANDBOX:
        return jsonify({"erro":"Apenas em sandbox"}), 403
    pag = Pagamento.query.get_or_404(pagamento_id)
    if pag.status != "aprovado":
        _processar_aprovado(pag)
    return jsonify({"ok": True,
                    "redirect": _abs("sucesso", pagamento_id=pagamento_id)})


# Proxy CEP — server-side, resolve qualquer problema de rede/CORS do browser
@app.route("/api/cep/<cep>")
def proxy_cep(cep):
    cep_num = "".join(c for c in cep if c.isdigit())
    if len(cep_num) != 8:
        return jsonify({"erro": "CEP deve ter 8 dígitos"}), 400
    try:
        r = requests.get(f"https://viacep.com.br/ws/{cep_num}/json/", timeout=6)
        d = r.json()
        if d.get("erro"):
            return jsonify({"erro": "CEP não encontrado"}), 404
        return jsonify({"logradouro": d.get("logradouro",""),
                        "bairro":     d.get("bairro",""),
                        "localidade": d.get("localidade",""),
                        "uf":         d.get("uf","")})
    except Exception as e:
        log.error("ViaCEP: %s", e)
        return jsonify({"erro": "Serviço de CEP indisponível — preencha manualmente"}), 503


@app.route("/admin")
def admin():
    if request.args.get("senha","") != os.getenv("ADMIN_SENHA","admin123"):
        return render_template("admin_login.html")
    rows = (db.session.query(Paciente, Pagamento)
            .outerjoin(Pagamento, Paciente.id == Pagamento.paciente_id)
            .order_by(Paciente.criado_em.desc()).all())
    return render_template("admin.html", registros=rows, sandbox=ASAAS_SANDBOX)


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        log.info("✅ Banco criado. Asaas: %s", "SANDBOX" if ASAAS_SANDBOX else "PRODUÇÃO")
    app.run(debug=os.getenv("FLASK_DEBUG","true").lower()=="true",
            host="0.0.0.0", port=5000)
