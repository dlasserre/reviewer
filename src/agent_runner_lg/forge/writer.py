"""Adaptateur GitHub — ECRITURE.

Classe SEPAREE du lecteur, et ce n'est pas un rangement : c'est la meme raison
qui fait qu'il y a deux jetons. Le lecteur ne connait pas le jeton d'ecriture et
n'a aucune methode qui ecrive ; construire un `GitHubWriter` est un geste
EXPLICITE, qui echoue tout de suite s'il n'y a pas de jeton. Une capacite
absente est une garantie plus solide qu'une capacite presente qu'on s'engage a
ne pas appeler.

── CE QUE CETTE CLASSE NE FERA JAMAIS ──────────────────────────────────────

Ni `mergePullRequest`, ni `closePullRequest`, ni `deleteRef`, ni la moindre
ecriture sur une branche partagee. Ce ne sont pas des oublis : le merge est une
decision humaine (`READY_FOR_HUMAN` existe pour ca), et les trois autres ne se
rattrapent pas. Ce qui n'est pas ecrit ici ne peut pas etre appele par erreur,
ni obtenu en detournant le prompt de l'agent.

── LE MARQUEUR EST POSE ICI, PAS PAR L'APPELANT ────────────────────────────

Chaque commentaire ecrit par le demon porte `AGENT_MARK`. C'est ce qui lui
permet de reconnaitre ses propres ecrits, alors qu'il publie sous le compte
d'un humain de confiance — sans quoi il se repondrait a lui-meme indefiniment.

Le marqueur est appose par le writer, jamais par l'appelant. Un marqueur qu'on
peut oublier de mettre est un marqueur qui sera oublie, et la panne serait la
pire possible : une boucle qui consomme le quota sans rien produire.

── IDEMPOTENCE ────────────────────────────────────────────────────────────

Le demon est declenche SUR NIVEAU : le meme passage peut se rejouer. Les
ecritures qui creent quelque chose (`create_pull`) verifient donc d'abord si
la chose existe deja, et les ecritures qui posent un etat (`resolve_thread`,
`add_label`) sont naturellement rejouables. Seul `reply_in_thread` ne l'est
pas — un commentaire poste deux fois fait deux commentaires — et c'est
l'appelant qui en porte la garde, via le marqueur d'attente.
"""

from __future__ import annotations

from typing import Any

import httpx

from agent_runner_lg.rules.machine import AGENT_MARK, ASK_MARK
from agent_runner_lg.forge.base import ForgeError

__all__ = ["GitHubWriter"]

API = "https://api.github.com/graphql"
REST = "https://api.github.com"

_REPONDRE = """
mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $thread, body: $body}) {
    comment { databaseId url }
  }
}
"""

_RESOUDRE = """
mutation($thread: ID!) {
  resolvePullRequestReviewThread(input: {threadId: $thread}) {
    thread { id isResolved }
  }
}
"""


class GitHubWriter:
    """Les ecritures du demon sur une forge GitHub.

    Le jeton attendu est celui d'ECRITURE. Un PAT fine-grained applique le meme
    jeu de permissions a tous les depots qu'il selectionne : c'est pour ca que
    lecture et ecriture sont deux jetons, et pour ca que le jeton d'ecriture ne
    selectionne QUE les depots `access: write`. Le refus devient serveur — meme
    un bug du demon ne peut pas ecrire ailleurs.
    """

    def __init__(self, org: str, token: str, *,
                 client: httpx.AsyncClient | None = None) -> None:
        if not token:
            # Refus a la CONSTRUCTION, pas au premier appel. Un writer sans
            # jeton qui echouerait a l'usage laisserait croire, entre les deux,
            # qu'une capacite d'ecriture existe.
            raise ForgeError(
                "GitHubWriter sans jeton d'ecriture : construire ce client est "
                "le geste qui donne l'ecriture, il ne peut pas etre a moitie fait."
            )
        self.org = org
        self._token = token
        self._client = client
        self._mine = client is None

    async def __aenter__(self) -> "GitHubWriter":
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
            "accept": "application/vnd.github+json",
            # GitHub REFUSE les requetes sans User-Agent, avec un 403 sans
            # message utile.
            "user-agent": "claude-agent-runner",
        }

    # ── Transport ──────────────────────────────────────────────────────────

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise ForgeError("client non ouvert : utiliser `async with GitHubWriter(...)`")
        try:
            r = await self._client.post(API, headers=self._headers,
                                        json={"query": query, "variables": variables})
        except httpx.HTTPError as e:
            raise ForgeError(f"GitHub injoignable : {e}") from e
        if r.status_code != 200:
            raise ForgeError(f"GitHub a repondu {r.status_code} : {r.text[:300]}")
        data = r.json()
        # GraphQL rend 200 MEME en erreur : ne jamais se fier au seul statut.
        if erreurs := data.get("errors"):
            messages = " | ".join(e.get("message", "?") for e in erreurs)
            raise ForgeError(f"GraphQL (ecriture) : {messages}")
        return data.get("data") or {}

    async def _rest(self, methode: str, chemin: str, *,
                    json_body: dict[str, Any] | None = None,
                    params: dict[str, Any] | None = None,
                    tolerer: tuple[int, ...] = ()) -> Any:
        """Appel REST. `tolerer` liste les statuts qui ne sont PAS des erreurs.

        Un 404 sur un retrait de label veut dire « il n'y etait pas », ce qui
        est l'etat voulu. Le distinguer d'une vraie panne evite qu'un passage
        entier tombe pour un non-evenement.
        """
        if self._client is None:
            raise ForgeError("client non ouvert : utiliser `async with GitHubWriter(...)`")
        try:
            r = await self._client.request(methode, f"{REST}{chemin}",
                                           headers=self._headers,
                                           json=json_body, params=params)
        except httpx.HTTPError as e:
            raise ForgeError(f"GitHub injoignable : {e}") from e
        if r.status_code in tolerer:
            return None
        if r.status_code >= 300:
            raise ForgeError(
                f"{methode} {chemin} a rendu {r.status_code} : {r.text[:300]}"
            )
        if not r.content:
            return None
        return r.json()

    # ── Fils de revue ──────────────────────────────────────────────────────

    def _signer(self, corps: str, *, attend_humain: bool = False) -> str:
        """Appose le ou les marqueurs. Le seul endroit qui le fasse.

        Les marqueurs sont mis en TETE, pas en queue. GitHub tronque l'apercu
        d'un commentaire long dans les notifications par courriel ; un marqueur
        en queue disparaitrait de l'apercu, et surtout un corps tronque en base
        — cas qui ne se produit pas aujourd'hui, mais rien ne le garantit —
        perdrait le marqueur en emportant avec lui la protection contre la
        boucle. En tete, il est dans les premiers octets.
        """
        marques = [AGENT_MARK]
        if attend_humain:
            marques.append(ASK_MARK)
        return "\n".join(marques) + "\n" + corps.strip() + "\n"

    async def reply_in_thread(self, thread_id: str, body: str, *,
                              awaiting_human: bool = False) -> int:
        """Repond DANS le fil. Rend l'identifiant du commentaire cree.

        `awaiting_human` marque la reponse comme une question. Tant qu'elle est
        le dernier mot du fil, la reconciliation ne reprendra pas ce fil : elle
        le comptera comme une attente. C'est ce marqueur qui empeche l'agent de
        reposer la meme question a chaque passage — et sa disparition, quand
        quelqu'un repond en dessous, qui le relance.
        """
        data = await self._graphql(_REPONDRE, {
            "thread": thread_id,
            "body": self._signer(body, attend_humain=awaiting_human),
        })
        commentaire = (
            (data.get("addPullRequestReviewThreadReply") or {}).get("comment") or {}
        )
        return int(commentaire.get("databaseId") or 0)

    async def resolve_thread(self, thread_id: str) -> None:
        """Marque le fil resolu.

        C'est le fil resolu qui compte comme solde, pas la reponse : un
        correctif pousse, argumente et vert dont le fil reste ouvert laisse la
        remarque comptee comme ouverte — et sur les depots de Damien, un fil
        ouvert suffit a retenir le merge.
        """
        await self._graphql(_RESOUDRE, {"thread": thread_id})

    # ── Commentaires et etiquettes de PR ───────────────────────────────────

    async def comment_on_pull(self, repo: str, number: int, body: str, *,
                              awaiting_human: bool = False) -> int:
        """Commentaire au niveau de la PR, quand aucun fil ne convient.

        Une CI rouge n'appartient a aucun fil de revue : il n'y a pas de
        remarque a laquelle repondre. Sans cette voie, l'agent n'aurait aucun
        moyen de signaler ce qui le bloque le plus souvent.
        """
        data = await self._rest(
            "POST", f"/repos/{self.org}/{repo}/issues/{number}/comments",
            json_body={"body": self._signer(body, attend_humain=awaiting_human)},
        )
        return int((data or {}).get("id") or 0)

    async def add_label(self, repo: str, number: int, label: str) -> None:
        await self._rest("POST", f"/repos/{self.org}/{repo}/issues/{number}/labels",
                         json_body={"labels": [label]})

    async def remove_label(self, repo: str, number: int, label: str) -> None:
        # 404 = le label n'y etait pas, ce qui est l'etat voulu.
        await self._rest("DELETE",
                         f"/repos/{self.org}/{repo}/issues/{number}/labels/{label}",
                         tolerer=(404,))

    # ── Issues ─────────────────────────────────────────────────────────────

    async def find_issue_by_marker(self, repo: str, marker: str) -> dict[str, Any] | None:
        """Issue portant ce marqueur, ouverte ou FERMEE, ou `None`.

        Le depot demande de chercher les doublons avant de creer, « y compris
        les issues fermees » : une issue close dit que le sujet a deja ete
        traite, et en rouvrir un double est pire que de ne rien creer.

        C'est un FILET, pas la cle d'idempotence. La cle est le numero d'issue
        garde en etat local : l'index de recherche de GitHub met plusieurs
        dizaines de secondes a voir une issue neuve, et deux passages
        rapproches en creeraient deux. Cette recherche ne sert donc qu'au cas
        ou l'etat local a disparu — base effacee, machine changee.
        """
        requete = f'repo:{self.org}/{repo} in:body "{marker}"'
        data = await self._rest("GET", "/search/issues",
                                params={"q": requete, "per_page": 5},
                                tolerer=(403, 422, 503))
        for element in (data or {}).get("items") or []:
            # L'API de recherche melange issues et PR : une PR porte
            # `pull_request`. Prendre une PR pour une issue ferait ecrire
            # « Fixes #<numero de PR> », qui ne lie rien.
            if "pull_request" in element:
                continue
            if marker in (element.get("body") or ""):
                return element
        return None

    async def create_issue(self, repo: str, *, title: str, body: str,
                           labels: tuple[str, ...] | list[str] = (),
                           assignee: str | None = None) -> dict[str, Any]:
        """Cree une issue. Rend l'objet REST (`number`, `html_url`…).

        `labels` et `assignee` sont passes a la CREATION plutot qu'ajoutes
        ensuite : un Project pilote par les evenements reagit a chacun, et
        trois appels produisent trois transitions de carte pour une seule
        intention.
        """
        charge: dict[str, Any] = {"title": title, "body": body}
        if labels:
            charge["labels"] = list(labels)
        if assignee:
            charge["assignees"] = [assignee.lstrip("@")]
        return await self._rest("POST", f"/repos/{self.org}/{repo}/issues",
                                json_body=charge)

    async def set_issue_type(self, repo: str, number: int, type_name: str) -> None:
        """Pose le TYPE d'issue — champ natif, pas une etiquette.

        Peut echouer sans que ce soit une panne : le catalogue des types vit sur
        l'organisation, et un PAT fine-grained y recoit 403 (mesure du
        27/08/2026). L'appelant traite l'echec comme une information a rendre,
        pas comme un motif d'arret : l'issue existe et elle est liee, seul un
        champ de classement manque.
        """
        await self._rest("PATCH", f"/repos/{self.org}/{repo}/issues/{number}",
                         json_body={"type": type_name})

    async def set_issue_priority(self, repo: str, number: int, *,
                                 field_id: int, value: str) -> None:
        """Pose la PRIORITE — champ d'issue au niveau organisation.

        La charge utile d'ECRITURE n'est pas symetrique de celle de lecture, et
        c'est le piege du sujet : la cle est `field_id` alors que la lecture
        rend `issue_field_id`, et la valeur est le NOM de l'option en chaine,
        pas son identifiant numerique. Les deux formes fausses rendent 422.
        """
        await self._rest(
            "PATCH", f"/repos/{self.org}/{repo}/issues/{number}",
            json_body={"issue_field_values": [{"field_id": field_id, "value": value}]},
        )

    # ── Pull requests ──────────────────────────────────────────────────────

    async def find_pull_by_head(self, repo: str, head: str) -> dict[str, Any] | None:
        """PR ouverte dont la tete est cette branche, ou `None`.

        Sert a l'IDEMPOTENCE de `create_pull`. Le demon rejoue ses passages :
        sans cette lecture, un second passage sur la meme branche produirait
        soit une erreur, soit une seconde PR — et deux PR pour un correctif est
        exactement le genre de desordre qu'on demande a l'humain de nettoyer.
        """
        lignes = await self._rest(
            "GET", f"/repos/{self.org}/{repo}/pulls",
            params={"state": "open", "head": f"{self.org}:{head}", "per_page": 10},
        )
        for pr in lignes or []:
            if (pr.get("head") or {}).get("ref") == head:
                return pr
        return None

    async def create_pull(self, repo: str, *, head: str, base: str,
                          title: str, body: str) -> dict[str, Any]:
        """Ouvre une PR — ou rend celle qui existe deja sur cette tete.

        Le corps NE PORTE PAS de marqueur d'agent : ce n'est pas un message
        dans un fil, et le marqueur ne sert qu'a reconnaitre ses propres
        commentaires. En poser un ici ferait apparaitre un commentaire HTML
        dans le corps d'une PR que Damien relira.
        """
        if existante := await self.find_pull_by_head(repo, head):
            return existante
        return await self._rest(
            "POST", f"/repos/{self.org}/{repo}/pulls",
            json_body={"head": head, "base": base, "title": title, "body": body},
        )
