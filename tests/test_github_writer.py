"""L'adaptateur d'ecriture publie-t-il ce qu'il faut, et RIEN de plus ?

Le transport est double (`httpx.MockTransport`), le reste est reel : la requete
partante est celle que GitHub recevrait, et c'est elle qu'on inspecte.

Deux familles de proprietes :

  - CE QU'IL FAIT : marquer chaque commentaire, ne pas creer deux fois la meme
    PR, distinguer un 404 tolerable d'une vraie panne ;
  - CE QU'IL NE PEUT PAS FAIRE : merger, fermer, forcer. Ces absences sont des
    garanties structurelles — on les teste comme telles, parce qu'une capacite
    ajoutee par distraction ne se verrait nulle part ailleurs.
"""

from __future__ import annotations

import json

import httpx
import pytest

from reviewer.forge.base import ForgeError
from reviewer.forge.writer import GitHubWriter
from reviewer.rules.machine import AGENT_MARK, ASK_MARK, Comment


def _writer(handler) -> GitHubWriter:
    return GitHubWriter("UneOrg", "jeton-de-test",
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _ok(charge) -> httpx.Response:
    return httpx.Response(200, json=charge)


def _capteur(charge=None):
    """Handler qui enregistre chaque requete et rend toujours la meme reponse."""
    vues: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vues.append(request)
        return _ok(charge if charge is not None else {
            "data": {"addPullRequestReviewThreadReply": {
                "comment": {"databaseId": 4242, "url": "https://x"}}}})

    handler.vues = vues
    return handler


def corps_de(request: httpx.Request) -> str:
    charge = json.loads(request.content.decode())
    if "variables" in charge:
        return charge["variables"].get("body", "")
    return charge.get("body", "")


# ── Le marqueur, la piece qui empeche la boucle ────────────────────────────


async def test_chaque_reponse_porte_le_marqueur_d_agent():
    # Sans lui, l'agent ne reconnait pas ses propres ecrits : il publie sous le
    # compte d'un humain de confiance, se relit comme du travail frais, et
    # repond a sa reponse indefiniment.
    h = _capteur()
    async with _writer(h) as w:
        await w.reply_in_thread("PRRT_1", "voila ce que j'ai fait")
    assert AGENT_MARK in corps_de(h.vues[0])


async def test_le_marqueur_d_attente_n_est_pose_QUE_sur_une_question():
    h = _capteur()
    async with _writer(h) as w:
        await w.reply_in_thread("PRRT_1", "corrige", awaiting_human=False)
        await w.reply_in_thread("PRRT_2", "je ne sais pas trancher", awaiting_human=True)

    assert ASK_MARK not in corps_de(h.vues[0])
    assert ASK_MARK in corps_de(h.vues[1])


async def test_les_marqueurs_sont_en_TETE_du_corps():
    # Un corps tronque — en apercu de notification, ou en base un jour — perdrait
    # un marqueur place en queue, et avec lui la protection contre la boucle.
    h = _capteur()
    async with _writer(h) as w:
        await w.reply_in_thread("PRRT_1", "texte", awaiting_human=True)
    corps = corps_de(h.vues[0])
    assert corps.startswith(AGENT_MARK)
    assert corps.index(ASK_MARK) < corps.index("texte")


async def test_le_marqueur_pose_par_le_writer_est_celui_que_la_machine_relit():
    # Les deux cotes doivent parler du meme marqueur. Le test les confronte
    # plutot que de repeter la chaine, ce qui laisserait passer une divergence.
    h = _capteur()
    async with _writer(h) as w:
        await w.reply_in_thread("PRRT_1", "texte", awaiting_human=True)
    relu = Comment(1, "dlasserre", corps_de(h.vues[0]))
    assert relu.from_agent and relu.asks_human


async def test_un_commentaire_de_PR_est_marque_aussi():
    h = _capteur({"id": 77})
    async with _writer(h) as w:
        await w.comment_on_pull("backend", 727, "la CI est rouge", awaiting_human=True)
    assert AGENT_MARK in corps_de(h.vues[0])
    assert ASK_MARK in corps_de(h.vues[0])


# ── Idempotence ────────────────────────────────────────────────────────────


async def test_une_PR_deja_ouverte_n_est_PAS_recreee():
    # Le demon rejoue ses passages. Sans cette lecture, un second passage sur la
    # meme branche produirait une erreur ou une seconde PR — et deux PR pour un
    # correctif, c'est du desordre qu'on demande a l'humain de nettoyer.
    appels: list[str] = []

    def handler(request):
        appels.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return _ok([{"head": {"ref": "fix/pr727-revue"},
                         "html_url": "https://github.com/UneOrg/backend/pull/900"}])
        return _ok({"html_url": "https://github.com/UneOrg/backend/pull/901"})

    async with _writer(handler) as w:
        pull = await w.create_pull("backend", head="fix/pr727-revue", base="dev",
                                   title="t", body="b")

    assert pull["html_url"].endswith("/900"), "on rend l'existante"
    assert not any(a.startswith("POST") for a in appels), "aucune creation"


async def test_une_branche_sans_PR_en_ouvre_une():
    def handler(request):
        if request.method == "GET":
            return _ok([])
        return _ok({"html_url": "https://github.com/UneOrg/backend/pull/901"})

    async with _writer(handler) as w:
        pull = await w.create_pull("backend", head="fix/pr727-revue", base="dev",
                                   title="t", body="b")
    assert pull["html_url"].endswith("/901")


async def test_une_autre_branche_ne_compte_pas_comme_la_notre():
    # GitHub filtre sur `head`, mais un filtre serveur qui elargirait rendrait
    # une PR d'une autre branche — qu'on prendrait pour la notre.
    def handler(request):
        if request.method == "GET":
            return _ok([{"head": {"ref": "fix/autre-chose"}, "html_url": "https://x/1"}])
        return _ok({"html_url": "https://x/2"})

    async with _writer(handler) as w:
        pull = await w.create_pull("backend", head="fix/pr727-revue", base="dev",
                                   title="t", body="b")
    assert pull["html_url"] == "https://x/2"


# ── Erreurs ────────────────────────────────────────────────────────────────


async def test_une_erreur_GraphQL_leve_MEME_avec_un_200():
    # GraphQL rend 200 en erreur : se fier au seul statut ferait passer un refus
    # de permission pour une publication reussie.
    def handler(_):
        return _ok({"errors": [{"message": "Resource not accessible"}]})

    async with _writer(handler) as w:
        with pytest.raises(ForgeError, match="Resource not accessible"):
            await w.reply_in_thread("PRRT_1", "x")


async def test_un_404_sur_le_RETRAIT_d_un_label_n_est_pas_une_erreur():
    # « Le label n'y etait pas » est l'etat voulu. Le traiter comme une panne
    # ferait tomber un passage entier pour un non-evenement.
    async with _writer(lambda _: httpx.Response(404, json={"message": "Label not found"})) as w:
        await w.remove_label("backend", 727, "needs-human")


async def test_un_404_sur_une_REPONSE_reste_une_erreur():
    async with _writer(lambda _: httpx.Response(404, json={"message": "Not Found"})) as w:
        with pytest.raises(ForgeError):
            await w.comment_on_pull("backend", 727, "x")


def test_un_writer_SANS_jeton_refuse_d_exister():
    # Refus a la CONSTRUCTION, pas au premier appel : entre les deux, un objet
    # existant laisserait croire qu'une capacite d'ecriture est disponible.
    with pytest.raises(ForgeError, match="sans jeton"):
        GitHubWriter("UneOrg", "")


# ── Ce que le writer ne peut pas faire ─────────────────────────────────────


@pytest.mark.parametrize("geste", ["merge", "close", "delete", "force", "approve"])
def test_aucune_capacite_de_merge_ni_de_fermeture(geste):
    # Le merge est une decision humaine (`READY_FOR_HUMAN` existe pour ca), et
    # les trois autres ne se rattrapent pas. Ce qui n'est pas ecrit ici ne peut
    # etre appele ni par erreur, ni en detournant le prompt de l'agent.
    publiques = [n for n in dir(GitHubWriter) if not n.startswith("_")]
    assert not [n for n in publiques if geste in n.lower()], publiques


# ── Les noms de mutation ────────────────────────────────────────────────────


def test_les_mutations_existent_VRAIMENT_chez_github():
    """Un nom de mutation faux ne leve QU'EN PRODUCTION.

    Le faux transport des tests rend ce qu'on lui dit de rendre : il ne lit pas
    le document. `resolvePullRequestReviewThread` a donc vecu ici jusqu'au
    31/08/2026 — la reponse partait, la resolution echouait, et chaque remarque
    corrigee restait comptee comme OUVERTE sur la forge. Un fil ouvert retient
    le merge.

    Cette liste a ete lue chez GitHub :

        gh api graphql -f query='{ __type(name:"Mutation"){fields{name}} }'

    L'egalite stricte est deliberee : si l'extraction cessait de trouver quoi
    que ce soit, une simple inclusion passerait a vide. Ajouter un nom ici est
    un GESTE — il vaut declaration qu'on est alle le verifier.
    """
    import re

    from reviewer.forge import writer as w

    CONNUES = {"addPullRequestReviewThreadReply", "resolveReviewThread"}

    docs = [v for k, v in vars(w).items()
            if k.isupper() and isinstance(v, str) and "mutation" in v]
    assert docs, "aucun document GraphQL trouve : ce test ne prouve plus rien"

    trouvees = set()
    for d in docs:
        trouvees |= set(re.findall(r"^\s+(\w+)\(input:", d, re.M))

    assert trouvees == CONNUES, (
        f"mutations du writer : {sorted(trouvees)}. Verifier chez GitHub avant "
        f"de modifier CONNUES.")


async def test_un_fil_resolu_appelle_la_BONNE_mutation():
    # Le nom voyage jusqu'a la requete : un test qui n'inspecte que le retour
    # du faux transport ne verrait pas une faute de frappe.
    from reviewer.forge.writer import GitHubWriter  # noqa: F401

    handler = _capteur({"data": {"resolveReviewThread": {
        "thread": {"id": "PRRT_1", "isResolved": True}}}})
    w = _writer(handler)
    await w.resolve_thread("PRRT_1")

    assert len(handler.vues) == 1
    corps = handler.vues[0].content.decode()
    assert "resolveReviewThread" in corps
    assert "resolvePullRequestReviewThread" not in corps
