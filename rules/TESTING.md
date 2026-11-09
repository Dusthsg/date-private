# Regra de Isolamento de Banco de Dados para Testes

Qualquer teste automatizado, manual ou script de verificação deve ser executado **exclusivamente em um banco de dados próprio e isolado** (`database/test_products.db` ou banco temporário/em memória).

## Diretrizes Obrigatórias:
1. **Nunca alterar o banco de produção**: `database/products.db` e o banco PostgreSQL (Neon) contêm os dados reais cadastrados pelo usuário e nunca devem ser modificados por rotinas de teste.
2. **Ambiente de Teste**: Definir sempre `os.environ["DATELIMIT_TESTING"] = "1"` e `os.environ["TESTING"] = "true"` antes de rodar baterias de teste.
3. **Desativação Automática de Nuvem**: Sob `DATELIMIT_TESTING=1`, o `is_postgres_configured()` retorna `False` automaticamente, prevenindo qualquer conexão, pull, push ou sincronização com a nuvem durante testes.
4. **Isolamento de Arquivo**: `SQLITE_PATH` aponta automaticamente para `database/test_products.db`, que está devidamente adicionado ao `.gitignore`.
