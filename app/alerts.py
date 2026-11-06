"""
Módulo de Alertas Automáticos de Validade
DateLimit — Controle de Validade (Reality)

Suporte a múltiplos canais:
- Telegram Bot API
- Webhook (Discord, Slack, WhatsApp Gateways, n8n, etc.)
- Agendamento diário automático pela manhã
- Disparo manual de teste com feedback em tempo real
"""

import json
import urllib.request
import urllib.parse
import urllib.error
import datetime
from app.db import (
    get_alert_settings,
    save_alert_settings,
    update_alert_sent_status,
    get_db_cursor
)


def get_alert_summary(include_vencidos=True, include_criticos=True, include_atencao=False) -> dict:
    """
    Coleta os produtos e lotes relevantes para o alerta matinal,
    calculando totais financeiros e agrupando por faixa de criticidade.
    """
    today = datetime.date.today()
    now_str = today.isoformat()
    critico_str = (today + datetime.timedelta(days=7)).isoformat()
    atencao_str = (today + datetime.timedelta(days=30)).isoformat()

    vencidos = []
    criticos = []
    atencao = []

    try:
        with get_db_cursor() as (cur, db_type):
            cur.execute("""
                SELECT p.name, l.quant, COALESCE(l.price, p.price, 0.0) as price, l.date_valid, l.id
                FROM product_lots l
                JOIN products p ON l.product_id = p.id
                WHERE l.quant > 0
                ORDER BY l.date_valid ASC, p.name ASC
            """)
            rows = cur.fetchall()
            for r in rows:
                p_name = r[0]
                quant = int(r[1] or 0)
                price = float(r[2] or 0.0)
                d_valid = str(r[3])
                lot_id = r[4]

                item_total = quant * price
                item = {
                    "lot_id": lot_id,
                    "name": p_name,
                    "quant": quant,
                    "price": price,
                    "date_valid": d_valid,
                    "item_total": item_total
                }

                if d_valid < now_str:
                    vencidos.append(item)
                elif d_valid <= critico_str:
                    try:
                        d_obj = datetime.date.fromisoformat(d_valid)
                        days_left = (d_obj - today).days
                    except Exception:
                        days_left = 0
                    item["days_left"] = days_left
                    criticos.append(item)
                elif d_valid <= atencao_str:
                    atencao.append(item)

    except Exception as e:
        print(f"⚠️ Erro ao calcular resumo de alerta: {e}")

    total_vencidos_val = sum(x["item_total"] for x in vencidos)
    total_criticos_val = sum(x["item_total"] for x in criticos)
    total_atencao_val = sum(x["item_total"] for x in atencao)

    weekday_names = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]

    return {
        "date_str": today.strftime("%d/%m/%Y"),
        "weekday_pt": weekday_names[today.weekday()],
        "vencidos": vencidos if include_vencidos else [],
        "criticos": criticos if include_criticos else [],
        "atencao": atencao if include_atencao else [],
        "vencidos_count": len(vencidos),
        "criticos_count": len(criticos),
        "atencao_count": len(atencao),
        "vencidos_val": total_vencidos_val,
        "criticos_val": total_criticos_val,
        "atencao_val": total_atencao_val,
        "total_risk_val": total_vencidos_val + total_criticos_val
    }


def format_alert_message(summary: dict, is_test: bool = False) -> str:
    """Gera texto formatado e profissional do alerta para Telegram ou Webhook."""
    lines = []
    
    if is_test:
        lines.append("🔔 <b>[TESTE DE ENVIO — DATELIMIT]</b>")
    else:
        lines.append("🛡️ <b>DateLimit • Alerta Diário de Validade</b>")
    
    lines.append(f"📅 {summary['weekday_pt']}, {summary['date_str']}")
    lines.append("🏢 Reality — Gestão de Validade\n")

    has_content = False

    # 1. Vencidos
    if summary.get("vencidos"):
        has_content = True
        lines.append(f"🚨 <b>PRODUTOS VENCIDOS ({len(summary['vencidos'])} itens — R$ {summary['vencidos_val']:.2f}):</b>")
        for it in summary["vencidos"][:15]:
            try:
                dv_br = datetime.date.fromisoformat(it['date_valid']).strftime('%d/%m/%Y')
            except Exception:
                dv_br = it['date_valid']
            lines.append(f"• <b>{it['name']}</b> ({it['quant']} un) — Venceu em {dv_br}")
        if len(summary["vencidos"]) > 15:
            lines.append(f"<i>...e mais {len(summary['vencidos']) - 15} itens vencidos.</i>")
        lines.append("")

    # 2. Críticos (7 dias)
    if summary.get("criticos"):
        has_content = True
        lines.append(f"⚠️ <b>CRÍTICO (Vencem em até 7 dias) ({len(summary['criticos'])} itens — R$ {summary['criticos_val']:.2f}):</b>")
        for it in summary["criticos"][:15]:
            try:
                dv_br = datetime.date.fromisoformat(it['date_valid']).strftime('%d/%m/%Y')
            except Exception:
                dv_br = it['date_valid']
            days = it.get('days_left', 0)
            day_text = "vence hoje!" if days == 0 else f"em {days} dia(s)"
            lines.append(f"• <b>{it['name']}</b> ({it['quant']} un) — {day_text} ({dv_br})")
        if len(summary["criticos"]) > 15:
            lines.append(f"<i>...e mais {len(summary['criticos']) - 15} itens críticos.</i>")
        lines.append("")

    # 3. Atenção (8 a 30 dias)
    if summary.get("atencao"):
        has_content = True
        lines.append(f"⏰ <b>ATENÇÃO (8 a 30 dias):</b> {len(summary['atencao'])} produto(s) (R$ {summary['atencao_val']:.2f})\n")

    if not has_content:
        lines.append("🎉 <b>Parabéns!</b> Não há nenhum produto vencido ou na faixa crítica hoje!")
        lines.append("Seu estoque está 100% em dia.\n")
    else:
        lines.append(f"💰 <b>Total sob risco iminente:</b> R$ {summary['total_risk_val']:.2f}")

    lines.append("\n👉 <i>Acesse o sistema DateLimit para conferência ou baixa rápida.</i>")
    return "\n".join(lines)


def send_telegram_alert(token: str, chat_id: str, message: str) -> tuple[bool, str]:
    """Envia mensagem formatada usando a API oficial do Telegram Bot."""
    if not token or not chat_id:
        return False, "Token do bot e ID do Chat são obrigatórios para envio via Telegram."

    url = f"https://api.telegram.org/bot{token.strip()}/sendMessage"
    payload = {
        "chat_id": chat_id.strip(),
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "DateLimit-Alerts/1.0"}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("ok"):
                return True, "Mensagem enviada com sucesso ao Telegram!"
            return False, f"Telegram retornou erro: {res_data.get('description', 'Erro desconhecido')}"
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(err_msg)
            return False, f"Erro Telegram HTTP {e.code}: {err_json.get('description', err_msg)}"
        except Exception:
            return False, f"Erro Telegram HTTP {e.code}: {err_msg}"
    except Exception as e:
        return False, f"Falha na conexão com Telegram: {str(e)}"


def send_webhook_alert(url: str, summary: dict, message: str) -> tuple[bool, str]:
    """Envia payload JSON estruturado para webhook customizado ou gateway WhatsApp."""
    if not url:
        return False, "URL do webhook é obrigatória."

    clean_text = message.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    payload = {
        "event": "datelimit_alert",
        "timestamp": datetime.datetime.now().isoformat(),
        "content": clean_text,
        "text": clean_text,
        "summary": {
            "vencidos_count": summary["vencidos_count"],
            "criticos_count": summary["criticos_count"],
            "atencao_count": summary["atencao_count"],
            "total_risk_val": summary["total_risk_val"]
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url.strip(),
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "DateLimit-Alerts/1.0"}
    )

    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            if resp.status in (200, 201, 202, 204):
                return True, f"Webhook disparado com sucesso (HTTP {resp.status})!"
            return False, f"Webhook respondeu com status {resp.status}"
    except Exception as e:
        return False, f"Falha ao enviar webhook: {str(e)}"


def dispatch_alert(is_test: bool = False) -> tuple[bool, str]:
    """
    Executa o disparo do alerta diário ou de teste de acordo com as configurações do banco.
    """
    settings = get_alert_settings()
    if not is_test and not settings.get("enabled"):
        return False, "Alertas automáticos desativados nas configurações."

    channel = settings.get("channel", "telegram")
    include_vencidos = settings.get("include_vencidos", True)
    include_criticos = settings.get("include_criticos", True)
    include_atencao = settings.get("include_atencao", False)

    summary = get_alert_summary(
        include_vencidos=include_vencidos,
        include_criticos=include_criticos,
        include_atencao=include_atencao
    )
    message = format_alert_message(summary, is_test=is_test)

    now_time_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    today_str = datetime.date.today().isoformat()

    if channel == "telegram":
        token = settings.get("telegram_token", "")
        chat_id = settings.get("telegram_chat_id", "")
        success, msg = send_telegram_alert(token, chat_id, message)
    elif channel == "webhook":
        url = settings.get("webhook_url", "")
        success, msg = send_webhook_alert(url, summary, message)
    else:
        success, msg = False, f"Canal de envio desconhecido: {channel}"

    status_entry = f"[{'TESTE' if is_test else 'AUTO'}] {now_time_str} - {'Sucesso: ' + msg if success else 'Falha: ' + msg}"
    update_alert_sent_status(today_str if success and not is_test else settings.get("last_sent_date", ""), status_entry)

    return success, msg


def check_and_run_scheduled_alert():
    """
    Função chamada periodicamente pelo scheduler em background thread.
    Compara o horário diário configurado com o relógio local e dispara apenas 1 vez por dia.
    """
    try:
        settings = get_alert_settings()
        if not settings.get("enabled"):
            return

        now = datetime.datetime.now()
        current_hm = now.strftime("%H:%M")
        target_hm = settings.get("daily_time", "07:30")
        today_str = now.strftime("%Y-%m-%d")

        if current_hm == target_hm and settings.get("last_sent_date") != today_str:
            print(f"🔔 [Scheduler] Horário de alerta atingido ({current_hm}). Disparando alerta diário...")
            success, msg = dispatch_alert(is_test=False)
            print(f"🔔 [Scheduler] Resultado do disparo diário: {msg}")
    except Exception as e:
        print(f"⚠️ [Scheduler] Erro na verificação de alerta agendado: {e}")
