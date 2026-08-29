# Le demon de revue, en conteneur.
#
# ── CE QUE CETTE IMAGE CONTIENT, ET POURQUOI ────────────────────────────────
#
# Python pour le demon, git parce qu'il derive des worktrees, et Node parce que
# les VERIFICATIONS d'un depot JavaScript tournent ici. C'est le point qui
# decide de la forme de l'image : le demon ne commite pas de code dont les
# tests echouent, donc il doit pouvoir les lancer.
#
# ── CE QU'ELLE NE CONTIENT PAS ──────────────────────────────────────────────
#
# Les dependances de VOS depots. Un `node_modules` ou un `.venv` construit sur
# l'hote ne se reutilise pas ici : binaires natifs, shims `.cmd` sous Windows,
# `Scripts/` au lieu de `bin/`. Il faut les installer DANS le conteneur, une
# fois, sur un volume. Le README dit comment.
#
# Le paquet `keyring` non plus : il n'y a aucune dorsale de trousseau dans un
# conteneur. Les secrets arrivent par variables d'environnement — c'est la forme
# `env:NOM` des references de configuration.

FROM python:3.12-slim AS base

ARG NODE_MAJOR=22

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl gnupg \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# L'agent ECRIT dans des worktrees derives de depots montes depuis l'hote. Un
# fichier cree par root y devient impossible a modifier ensuite depuis l'hote,
# sous Linux. L'UID est donc un argument de construction : le faire correspondre
# a celui de l'hote evite tout un genre de pannes qui n'ont l'air de rien.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" runner 2>/dev/null || true \
 && useradd -m -u "${UID}" -g "${GID}" -s /bin/bash runner 2>/dev/null || true

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# `safe.directory` : les depots sont montes depuis l'hote et appartiennent a un
# autre UID. Sans cela git refuse d'y travailler — et le message parle de
# « dubious ownership », ce qui n'evoque rien quand on cherche pourquoi un
# worktree ne se cree pas.
RUN git config --system --add safe.directory '*'

USER runner
WORKDIR /config

# La console. Le demon ecoute sur 0.0.0.0 DANS le conteneur — la frontiere est
# le namespace reseau — et la publication cote hote doit rester sur 127.0.0.1.
EXPOSE 8788

# `check` avant tout : il dit ce qui manque pour AGIR, pas seulement pour
# demarrer. Un conteneur qui repond « healthy » alors qu'aucun jeton n'est pose
# est un conteneur qui ment.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8788/health', timeout=5).status==200 else 1)"

ENTRYPOINT ["reviewer"]
CMD ["-c", "/config/runner.yaml", "serve"]
