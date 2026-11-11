"""
Configuração do Gunicorn para execução de alta performance no Render.
Garante inicialização correta de conexões e threads nos processos filhos pós-fork.
"""

def post_fork(server, worker):
    """Executado dentro do processo worker Gunicorn logo após o fork no Linux."""
    try:
        from app.db import init_db
        init_db()
        server.log.info("Worker inicializado com sucesso com init_db()")
    except Exception as e:
        server.log.error(f"Erro no hook post_fork ao inicializar banco: {e}")
