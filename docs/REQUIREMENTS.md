# Requisitos do DateLimit

Este documento centraliza os requisitos funcionais (RF) e não funcionais (RNF) do DateLimit.

## Requisitos Funcionais

| ID | Requisito | Status |
|---|---|---|
| RF01 | CRUD de Produtos e Lotes | Planejado / em desenvolvimento |
| RF02 | Atualização Dinâmica de Lotes | Planejado |
| RF03 | Controle e Monitoramento de Validade | Em desenvolvimento |
| RF04 | Busca e Filtragem Avançada | Em desenvolvimento |
| RF05 | Manipulação e Exportação de Planilhas | Planejado |
| RF06 | Interface Gráfica Responsiva (Web/App) | Futuro |
| RF07 | Launcher | Futuro |
| RF08 | Download Direto via Interface | Futuro |
| RF09 | Agendamento Periódico de Relatórios | Futuro |
| RF10 | Disparo de Alertas Automáticos | Futuro |
| RF11 | Catálogo de Status Padrão | Futuro |
| RF12 | Criação de Status Personalizados | Futuro |
| RF13 | Atribuição Automática por Regras | Futuro |
| RF14 | Atribuição e Bloqueio Manual | Futuro |
| RF15 | Controle de Localização Física | Futuro |
| RF16 | Relatório de Reposição Preventiva | Futuro |
| RF17 | Histórico de Transferências Internas | Futuro |
| RF18 | Registro Histórico de Movimentações | Futuro |
| RF19 | Previsão de Risco de Vencimento | Futuro |
| RF20 | Recomendação Prescritiva | Futuro |
| RF21 | Previsão de Demanda e Sugestão de Reposição | Futuro |
| RF22 | Dashboard de Métricas Preditivas e Impacto Financeiro | Futuro |

### RF01 — CRUD de Produtos e Lotes
Permitir cadastrar, consultar, atualizar e excluir produtos e lotes.

### RF02 — Atualização Dinâmica de Lotes
Permitir atualizar individualmente atributos como quantidade, preço, validade, localização e status.

### RF03 — Controle e Monitoramento de Validade
Calcular e acompanhar a validade dos lotes em relação à data atual.

### RF04 — Busca e Filtragem Avançada
Permitir consultas por código, nome, intervalo de validade, localização e lotes críticos.

### RF05 — Manipulação e Exportação de Planilhas
Permitir importar/manipular dados e exportar relatórios em `.xlsx` e `.csv`.

### RF06 — Interface Gráfica Responsiva
Disponibilizar interface web/aplicativo com dashboards, cadastros e consultas adaptáveis a desktop, tablet e dispositivos móveis.

### RF07 — Launcher
Fornecer mecanismo simples para configurar o ambiente, iniciar o serviço e abrir a interface.

### RF08 — Download Direto via Interface
Permitir baixar relatórios diretamente pela interface.

### RF09 — Agendamento Periódico de Relatórios
Permitir gerar relatórios periódicos, como relatórios semanais de lotes críticos.

### RF10 — Disparo de Alertas Automáticos
Notificar automaticamente situações críticas, como lotes próximos do vencimento ou já vencidos.

### RF11 — Catálogo de Status Padrão
Disponibilizar estados como Normal, Atenção, Crítico e Vencido.

### RF12 — Criação de Status Personalizados
Permitir que administradores criem, editem e desativem status personalizados, incluindo nome, descrição e cor.

### RF13 — Atribuição Automática por Regras
Permitir regras de negócio para atribuição automática de status.

### RF14 — Atribuição e Bloqueio Manual
Permitir atribuição manual de status e bloqueio contra sobrescrita automática.

### RF15 — Controle de Localização Física
Representar locais como gôndola/loja, galpão/depósito e câmara fria.

### RF16 — Relatório de Reposição Preventiva
Identificar produtos em estoque de armazenamento que precisam de reposição preventiva, ordenados por validade.

### RF17 — Histórico de Transferências Internas
Registrar mudanças de localização, data, operador e quantidade.

### RF18 — Histórico de Movimentações
Registrar saídas de estoque com timestamp, quantidade, lote e motivo, como venda, avaria, descarte por vencimento ou uso interno.

### RF19 — Previsão de Risco de Vencimento
Estimar velocidade de saída e quantidade potencialmente restante no vencimento.

### RF20 — Recomendação Prescritiva
Sugerir ações como desconto ou transferência com base no excedente projetado.

### RF21 — Previsão de Demanda e Reposição
Estimar demanda considerando histórico, tendências e sazonalidade e sugerir volume de compra.

### RF22 — Dashboard Preditivo e Impacto Financeiro
Exibir métricas preditivas, risco financeiro e estimativas de perdas evitadas.

## Requisitos Não Funcionais

| ID | Requisito | Status |
|---|---|---|
| RNF01 | PostgreSQL e acesso concorrente | Futuro |
| RNF02 | Repository Pattern e independência do banco | Em desenvolvimento |
| RNF03 | Integridade referencial e concorrência | Futuro |
| RNF04 | Interface responsiva e touch-friendly | Futuro |
| RNF05 | Configuração simples do ambiente | Em preparação |
| RNF06 | Entrada por toque rápida | Futuro |
| RNF07 | Jobs assíncronos em segundo plano | Futuro |
| RNF08 | Consultas de inventário abaixo de 1 segundo | Futuro |
| RNF09 | Logs estruturados e auditoria | Futuro |
| RNF10 | Isolamento de ML das transações operacionais | Futuro |
| RNF11 | Fallback para produtos com menos de 30 dias de histórico | Futuro |
| RNF12 | Predições interpretáveis | Futuro |

### RNF01 — PostgreSQL e concorrência
A arquitetura deve permitir migração de SQLite para PostgreSQL para suportar acesso concorrente.

### RNF02 — Repository Pattern
A camada de persistência deve desacoplar regras de negócio e consultas do dialeto específico do banco, permitindo evolução de SQLite para PostgreSQL e, se necessário, serviços compatíveis como Supabase ou Neon.

### RNF03 — Integridade e concorrência
O sistema deve preservar integridade referencial e utilizar transações e isolamento adequados.

### RNF04 — Responsividade
A interface deve ser responsiva e adequada a dispositivos touch.

### RNF05 — Configuração
A instalação e inicialização devem exigir o mínimo possível de configuração manual.

### RNF06 — Entrada rápida
Operações de entrada devem responder rapidamente, especialmente em dispositivos touch.

### RNF07 — Processamento assíncrono
Tarefas agendadas e processos demorados devem poder executar em segundo plano.

### RNF08 — Desempenho
Consultas de inventário devem permanecer abaixo de 1 segundo mesmo com dezenas de milhares de lotes, dentro de condições operacionais definidas.

### RNF09 — Logs e auditoria
O sistema deve manter logs estruturados para eventos relevantes, incluindo inicialização, erros, notificações e movimentações críticas.

### RNF10 — Isolamento de ML
Treinamento e inferência de modelos não devem bloquear ou comprometer as transações operacionais.

### RNF11 — Cold start
Produtos com menos de 30 dias de histórico devem possuir estratégia de fallback para previsões.

### RNF12 — Interpretabilidade
As previsões devem apresentar fatores relevantes, como média de saída, estoque atual, dias até o vencimento e excedente estimado.

## Critérios de status
- **Em desenvolvimento:** requisito com implementação parcial.
- **Planejado:** requisito previsto para evolução próxima.
- **Futuro:** requisito dependente de fases posteriores.
- O status representa planejamento arquitetural, não o estado exato de cada issue.

## Rastreabilidade
Cada RF/RNF possui uma issue correspondente no GitHub. As issues acompanham a implementação; este documento é a referência consolidada dos requisitos.