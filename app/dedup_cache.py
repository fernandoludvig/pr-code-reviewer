"""Cache simples em memória para evitar reprocessar o mesmo commit de um PR.

⚠️ Este cache é APENAS em memória (um dict no processo) e é resetado se o
servidor reiniciar. É suficiente para o escopo de portfólio; em produção real
seria substituído por Redis ou um banco de dados compartilhado entre instâncias.
"""

import time


class TTLCache:
    """Cache de chaves com expiração por tempo (TTL).

    Uso típico: `seen(key)` faz check-and-set atômico (sob o GIL, sem await
    interno) — retorna True se a chave JÁ tinha sido vista dentro do TTL; caso
    contrário, registra a chave e retorna False.
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, float] = {}

    def _purge(self, now: float) -> None:
        expirados = [k for k, t in self._store.items() if now - t > self._ttl]
        for k in expirados:
            del self._store[k]

    def seen(self, key: str) -> bool:
        """Retorna True se a chave já foi vista (e ainda dentro do TTL).

        Se não foi vista, registra o timestamp atual e retorna False.
        """
        now = time.monotonic()
        self._purge(now)
        if key in self._store:
            return True
        self._store[key] = now
        return False
