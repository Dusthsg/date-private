# Roadmap do DateLimit

O roadmap organiza a evolução do DateLimit por dependências técnicas e valor funcional. As fases podem ser ajustadas conforme desenvolvimento e testes.

## Visão geral
```text
Núcleo CLI
   ↓
Persistência e arquitetura
   ↓
Interface Web
   ↓
Automação
   ↓
Gestão avançada de estoque
   ↓
Histórico de movimentações
   ↓
Analytics e ML
```

## Fase 1 — Núcleo do inventário
**Objetivo:** consolidar produtos, lotes, validade e consultas.

Escopo: RF01–RF05.

Também inclui:
- estabilização da CLI;
- testes das operações existentes;
- definição clara de entradas, saídas e erros.

**Resultado esperado:** CLI confiável para operações básicas de inventário.

## Fase 2 — Arquitetura e persistência
**Objetivo:** separar responsabilidades e preparar o sistema para crescimento.

Escopo: RNF02, RNF03 e preparação para RNF01.

Arquitetura prevista:
```text
View
 ↓
Controller
 ↓
Service
 ↓
Repository
 ↓
Database
```
- concluir migração de `crud/` para `repositories/`;
- introduzir Views;
- reduzir responsabilidades do Controller;
- introduzir Services quando houver regras suficientes;
- testar persistência;
- registrar decisões arquiteturais.

**Resultado esperado:** núcleo desacoplado do banco específico.

## Fase 3 — Interface
**Objetivo:** transformar o núcleo em uma aplicação utilizável por diferentes dispositivos.

Escopo: RF06–RF08 e RNF04–RNF06.

**Resultado esperado:** interface capaz de consumir o núcleo sem depender da CLI.

## Fase 4 — Automação
**Objetivo:** reduzir operações manuais e tornar o sistema proativo.

Escopo: RF09–RF10, RNF07 e RNF09.

**Resultado esperado:** relatórios e alertas automáticos com rastreabilidade.

## Fase 5 — Status e localização
**Objetivo:** representar melhor o estado operacional do estoque.

Escopo: RF11–RF17.

Esta fase concentra regras de negócio mais complexas e pode justificar Service/Rule Engine.

**Resultado esperado:** sistema capaz de representar e automatizar decisões operacionais do estoque.

## Fase 6 — Histórico operacional
**Objetivo:** construir uma base histórica confiável para análise.

Escopo: RF18.

Prioridades:
- registrar eventos de forma consistente;
- preservar timestamps e identificação do lote;
- registrar motivo e quantidade;
- evitar alterações destrutivas do histórico;
- garantir consultas eficientes.

**Resultado esperado:** histórico suficiente para análises posteriores.

> Esta fase deve preceder a implementação séria dos recursos preditivos. Sem histórico confiável, previsões terão uma base fraca.

## Fase 7 — Analytics e Machine Learning
**Objetivo:** transformar o histórico operacional em previsões e recomendações.

Escopo: RF19–RF22 e RNF08, RNF10–RNF12.

Ordem conceitual:
```text
Dados históricos
      ↓
Preparação dos dados
      ↓
Métricas estatísticas
      ↓
Modelos preditivos
      ↓
Estimativa de risco
      ↓
Recomendações
      ↓
Dashboard
```

**Resultado esperado:** recursos analíticos baseados em dados reais do sistema.

## Fase 8 — Escala e evolução
Possíveis objetivos quando o uso justificar:
- migração completa para PostgreSQL;
- otimização e índices;
- análise de desempenho;
- execução distribuída de tarefas;
- observabilidade avançada;
- evolução de segurança;
- melhoria do launcher e implantação.

## Regra do roadmap
O DateLimit não deve implementar antecipadamente toda a arquitetura futura.

Cada fase deve criar a base necessária para a próxima, sem adicionar abstrações que ainda não tenham necessidade concreta.

**Prioridade:** funcionalidade real → separação de responsabilidades → testes → escala → automação → inteligência.