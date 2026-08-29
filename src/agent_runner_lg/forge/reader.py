"""Adaptateur GitHub — LECTURE SEULE.

Une requete GraphQL par depot rend tout ce dont `decide` a besoin : les PR
ouvertes, leurs checks, et leurs fils de revue. Le REST equivalent demanderait
trois appels par PR, et la doc du depot rappelle qu'il faut `--paginate` sous
peine de s'arreter a 30 elements — en manquant precisement les plus recents.

Cette classe n'expose AUCUNE ecriture. Ce n'est pas un oubli : en lot 1, le
demon ne doit pas pouvoir ecrire, et une capacite absente est une garantie plus
solide qu'une capacite presente qu'on s'engage a ne pas appeler.

── DEUX SURFACES DE CHECK, PAS UNE ─────────────────────────────────────────
`statusCheckRollup` melange deux types : les `CheckRun` (GitHub Actions) et les
`StatusContext` (services externes qui poussent un statut de commit — Snyk chez
Plantifia). Ils n'ont ni le meme vocabulaire ni les memes champs. N'en lire
qu'un donne un « tout est vert » qui ignore la moitie des signaux.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from agent_runner_lg.rules.machine import Check, Comment, PullSnapshot, Thread, compile_ignored
from agent_runner_lg.forge.base import ForgeError

__all__ = ["GitHubReader"]

API = "https://api.github.com/graphql"

_QUERY = """
query($owner: String!, $name: String!, $prs: Int!, $threads: Int!, $msgs: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $prs, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        isDraft
        merged
        closed
        headRefName
        baseRefName
        headRefOid
        commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name status conclusion completedAt }
          ... on StatusContext { context state createdAt }
        } } } } } }
        reviewThreads(first: $threads) {
          nodes {
            id
            isResolved
            comments(first: $msgs) { nodes {
              databaseId
              author { login }
              body
              path
              line
              originalLine
            } }
          }
        }
      }
    }
  }
}
"""

# Le CHAMP refuse -> la permission a cocher sur le jeton. Sans cette table, un
# « Resource not accessible by personal access token » oblige a deviner laquelle
# des permissions manque parmi celles que la requete touche — et on la devine
# mal. GitHub donne le champ dans `path` ; il ne restait qu'a le traduire.
_PERMISSION = (
    ("statusCheckRollup", "Checks: Read (et Commit statuses: Read)"),
    ("reviewThreads", "Pull requests: Read"),
    ("pullRequests", "Pull requests: Read"),
)


def _est_erreur_de_checks(e: dict[str, Any]) -> bool:
    """L'erreur ne porte-t-elle QUE sur l'etat de la CI ?

    Les jetons fine-grained n'offrent pas la permission `Checks` (constate le
    25/08/2026 : absente du selecteur, alors que GitHub repond bien
    `x-accepted-github-permissions: checks=read`). Le reste de la requete
    passe ; seul `statusCheckRollup` est refuse. Plutot que de perdre le depot
    entier, on isole ce cas et on relit la CI par l'API Actions, que la
    permission `Actions: Read` — elle, disponible — couvre.
    """
    return "statusCheckRollup" in ".".join(str(p) for p in (e.get("path") or []))


def _permission_probable(chemin: str) -> str | None:
    for champ, permission in _PERMISSION:
        if champ in chemin:
            return permission
    return None


def _erreurs(erreurs: list[dict[str, Any]]) -> str:
    """Rend une erreur GraphQL ACTIONNABLE.

    GitHub renvoie le message ET le `path` du champ refuse. Ne garder que le
    message — ce que faisait ce code — retire la seule information qui dit quoi
    corriger : « Resource not accessible by personal access token » est vrai
    pour une dizaine de permissions differentes.

    Les index de liste sont effaces du chemin (`nodes.0` -> `nodes[]`) et les
    doublons regroupes : un champ refuse sur sept contextes de CI produisait
    sept fois la meme phrase, ce qui noie la seule ligne a lire.
    """
    vus: dict[str, int] = {}
    for e in erreurs:
        msg = e.get("message", "?")
        chemin = ".".join(
            "[]" if isinstance(p, int) else str(p) for p in (e.get("path") or [])
        ).replace(".[]", "[]")
        if chemin:
            msg = f"{msg} (champ : {chemin})"
            if permission := _permission_probable(chemin):
                msg = f"{msg} -> permission a ajouter au jeton : {permission}"
        vus[msg] = vus.get(msg, 0) + 1
    return " | ".join(
        m if n == 1 else f"{m} [x{n}]" for m, n in vus.items()
    )


# `StatusContext.state` -> (status, conclusion) du vocabulaire `CheckRun`, pour
# que la machine n'ait qu'un seul vocabulaire a connaitre.
_ETAT_CONTEXTE = {
    "SUCCESS": ("completed", "success"),
    "FAILURE": ("completed", "failure"),
    "ERROR": ("completed", "failure"),
    "PENDING": ("in_progress", None),
    "EXPECTED": ("queued", None),
}


def _horodatage(valeur: str | None) -> datetime | None:
    if not valeur:
        return None
    try:
        return datetime.fromisoformat(valeur.replace("Z", "+00:00"))
    except ValueError:
        return None


def _checks(rollup: dict[str, Any] | None,
            ignored: "re.Pattern[str] | None" = None) -> tuple[tuple[Check, ...], datetime | None]:
    """Rend les checks, et l'instant ou le DERNIER a conclu.

    Cet instant est ce qui ouvre la fenetre de revue. Le prendre au plus tard
    est deliberé : une PR n'est prete a etre relue que quand tout a conclu.
    """
    if not rollup:
        return (), None
    motif = ignored or compile_ignored(None)
    checks: list[Check] = []
    dernier: datetime | None = None
    for n in rollup.get("contexts", {}).get("nodes", []) or []:
        if n.get("__typename") == "CheckRun":
            c = Check(
                name=n.get("name") or "?",
                status=(n.get("status") or "").lower(),
                conclusion=(n.get("conclusion") or "").lower() or None,
            )
            fin = _horodatage(n.get("completedAt"))
        elif n.get("__typename") == "StatusContext":
            statut, conclusion = _ETAT_CONTEXTE.get((n.get("state") or "").upper(),
                                                    ("completed", None))
            c = Check(name=n.get("context") or "?", status=statut, conclusion=conclusion)
            fin = _horodatage(n.get("createdAt"))
        else:
            continue
        checks.append(c)
        # Seuls les checks qui COMPTENT bornent la fenetre : attendre la fin
        # d'un `release` saute n'aurait aucun sens.
        if fin and not c.pending and not c.ignored_by(motif) and (dernier is None or fin > dernier):
            dernier = fin
    return tuple(checks), dernier


def _threads(bloc: dict[str, Any] | None) -> tuple[Thread, ...]:
    """Les fils de revue, AVEC leur suite.

    L'ouverture du fil garde son role — c'est elle qui porte l'ancrage et la
    severite. Mais c'est la SUITE qui dit s'il reste du travail : une remarque
    a laquelle l'agent a repondu, une question a laquelle l'humain a repondu et
    une remarque jamais traitee ont exactement la meme ouverture.
    """
    out: list[Thread] = []
    for t in (bloc or {}).get("nodes", []) or []:
        commentaires = (t.get("comments") or {}).get("nodes") or []
        if not commentaires:
            continue
        c = commentaires[0]
        auteur = (c.get("author") or {}).get("login") or ""
        out.append(Thread(
            id=t.get("id") or "",
            comment_id=int(c.get("databaseId") or 0),
            author=auteur,
            resolved=bool(t.get("isResolved")),
            body=c.get("body") or "",
            path=c.get("path"),
            # `line` est nul sur un fil devenu obsolete ; `originalLine` garde
            # l'ancrage d'origine. Sans ce repli, une remarque re-ancree perd sa
            # position et le journal ne dit plus ou elle porte.
            line=c.get("line") if c.get("line") is not None else c.get("originalLine"),
            comments=tuple(
                Comment(
                    id=int(m.get("databaseId") or 0),
                    author=(m.get("author") or {}).get("login") or "",
                    body=m.get("body") or "",
                )
                for m in commentaires
            ),
        ))
    return tuple(out)


class GitHubReader:
    """Lecture des PR d'une organisation. Aucun chemin d'ecriture.

    Le jeton attendu ici est celui de LECTURE. Un PAT fine-grained applique le
    meme jeu de permissions a tous les depots qu'il selectionne : c'est pourquoi
    lecture et ecriture sont deux jetons, et pourquoi ce client ne connait que
    le premier.
    """

    def __init__(self, org: str, token: str, *, client: httpx.AsyncClient | None = None,
                 max_prs: int = 30, max_threads: int = 60, max_messages: int = 20,
                 ignored_checks: re.Pattern[str] | None = None) -> None:
        self.org = org
        self._token = token
        self._ignored = ignored_checks
        self._client = client
        self._mine = client is None
        self.max_prs = max_prs
        self.max_threads = max_threads
        self.max_messages = max_messages

    async def __aenter__(self) -> "GitHubReader":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._mine and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token}",
            "content-type": "application/json",
            # GitHub REFUSE les requetes sans User-Agent, avec un 403 sans
            # message utile.
            "user-agent": "claude-agent-runner",
        }

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise ForgeError("client non ouvert : utiliser `async with GitHubReader(...)`")
        try:
            r = await self._client.post(API, headers=self._headers,
                                        json={"query": query, "variables": variables})
        except httpx.HTTPError as e:
            raise ForgeError(f"GitHub injoignable : {e}") from e
        if r.status_code != 200:
            raise ForgeError(f"GitHub a repondu {r.status_code} : {r.text[:300]}")
        data = r.json()
        # GraphQL rend 200 MEME en erreur : ne jamais se fier au seul statut.
        erreurs = data.get("errors") or []
        # Une erreur confinee aux checks n'est pas fatale : le reste de la
        # reponse — PR, fils de revue — est exploitable, et l'etat de la CI se
        # relit autrement (API Actions). On ne tolere QUE ce cas ; toute autre
        # erreur reste bloquante, parce qu'on ne saurait pas ce qui manque.
        autres = [e for e in erreurs if not _est_erreur_de_checks(e)]
        if autres:
            raise ForgeError("GraphQL : " + _erreurs(autres))
        return data.get("data") or {}, bool(erreurs)

    async def _checks_par_actions(self, repo: str, sha: str
                                  ) -> tuple[tuple[Check, ...], datetime | None, bool]:
        """Relit l'etat de la CI par l'API Actions, quand GraphQL l'a refuse.

        Un `workflow run` porte la meme information qu'un `CheckRun` — nom,
        statut, conclusion, instant de fin — pour une CI qui passe par GitHub
        Actions.

        CE QU'ELLE NE VOIT PAS : les `StatusContext` poses par un service
        externe, Snyk chez Plantifia (cf. l'en-tete de ce module). Cette voie
        est donc un REPLI, pas un equivalent : quand la permission `Checks` est
        disponible, la requete GraphQL reste la bonne source.

        Le troisieme membre rendu dit si la lecture a REUSSI. Rendre un tuple
        vide en cas d'echec dirait « aucun check », donc « rien de rouge ».
        """
        if self._client is None:
            raise ForgeError("client non ouvert")
        url = f"https://api.github.com/repos/{self.org}/{repo}/actions/runs"
        try:
            r = await self._client.get(url, headers=self._headers,
                                       params={"head_sha": sha, "per_page": 100})
        except httpx.HTTPError:
            return (), None, False
        if r.status_code != 200:
            return (), None, False
        motif = self._ignored or compile_ignored(None)
        checks: list[Check] = []
        dernier: datetime | None = None
        for run in (r.json() or {}).get("workflow_runs") or []:
            c = Check(
                name=run.get("name") or run.get("display_title") or "?",
                status=(run.get("status") or "").lower(),
                conclusion=(run.get("conclusion") or "").lower() or None,
            )
            checks.append(c)
            # Meme regle que `_checks` : seuls les checks qui COMPTENT bornent
            # la fenetre de revue. On les garde tous dans la liste — le filtrage
            # appartient a `relevant_checks`, en aval.
            fin = _horodatage(run.get("updated_at"))
            if fin and not c.pending and not c.ignored_by(motif) and (
                    dernier is None or fin > dernier):
                dernier = fin
        return tuple(checks), dernier, True

    async def open_pulls(self, repo: str) -> list[PullSnapshot]:
        data, checks_refuses = await self._graphql(_QUERY, {
            "owner": self.org, "name": repo,
            "prs": self.max_prs, "threads": self.max_threads,
            "msgs": self.max_messages,
        })
        depot = data.get("repository")
        if depot is None:
            raise ForgeError(f"depot introuvable ou invisible pour ce jeton : {self.org}/{repo}")

        out: list[PullSnapshot] = []
        for pr in (depot.get("pullRequests") or {}).get("nodes", []) or []:
            lisible = True
            if checks_refuses:
                checks, conclus, lisible = await self._checks_par_actions(
                    repo, pr.get("headRefOid") or "")
            else:
                commits = ((pr.get("commits") or {}).get("nodes") or [])
                rollup = commits[0]["commit"].get("statusCheckRollup") if commits else None
                checks, conclus = _checks(rollup, self._ignored)
            out.append(PullSnapshot(
                number=pr["number"],
                repo=repo,
                head_sha=pr.get("headRefOid") or "",
                head_ref=pr.get("headRefName") or "",
                base_ref=pr.get("baseRefName") or "",
                draft=bool(pr.get("isDraft")),
                merged=bool(pr.get("merged")),
                closed=bool(pr.get("closed")),
                checks=checks,
                threads=_threads(pr.get("reviewThreads")),
                checks_concluded_at=conclus,
                checks_readable=lisible,
            ))
        return out
