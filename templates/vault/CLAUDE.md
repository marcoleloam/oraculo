# Instruções de memória para Claude

Leia `AGENTS.md` na raiz deste vault e o `schema/config.md` da wiki selecionada antes de operar. O canônico tem um único publicador: envie propostas pelo Oráculo usando o grant da tarefa, sem editar raw, wiki, índice ou log diretamente.

Na revisão do Obsidian, trabalhe exclusivamente em `reviews/<session_id>/wiki` selecionada, conserve IDs e nomes exclusivos da sessão e use links completos desde a raiz do vault. Nunca use aliases para resolver identidade de cliente. Não altere raw nem páginas reviewed:true. Não misture projetos; shared contém apenas aprendizado sanitizado. Não delegue nem use APIs pagas como fallback.

A operação headless usa T3 e planos existentes. A compatibilidade do plugin 1.28.0 é estrutural; validação E2E de autenticação e resolução no Obsidian permanece necessária. Regras completas e fluxo de conflito estão em `AGENTS.md`.
