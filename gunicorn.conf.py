# gunicorn.conf.py — configuração de produção para o Render (Free tier)
#
# Workers: 2 processos independentes
#   → se um travar o outro continua respondendo
# Threads: 4 por worker
#   → permite múltiplas requisições simultâneas sem bloquear
#   → ideal para o PokerCalc: cálculos rodam em threads separadas (threading.Thread)
# Timeout: 120s
#   → evita que cálculos de 100k simulações sejam cancelados
# Worker class: gthread
#   → combina processos + threads, melhor para I/O + CPU misto

workers     = 2
threads     = 4
worker_class = 'gthread'
timeout     = 120
keepalive   = 5
max_requests = 1000          # reinicia worker após 1000 req (evita memory leak)
max_requests_jitter = 100    # aleatoriza o reinício para não travar tudo junto
accesslog   = '-'            # loga acessos no stdout (visível no Render)
errorlog    = '-'
loglevel    = 'info'
