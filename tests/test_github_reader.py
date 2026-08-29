"""L'adaptateur GitHub lit-il ce que GitHub rend VRAIMENT ?

Le fixture `pr400.json` est une reponse GraphQL reelle, capturee sur
`Insectorize/frontend#400`. C'est le point : une reponse fabriquee a la main
reproduit ce qu'on croit que l'API rend, et un test ecrit sur cette croyance
valide la croyance, pas le code.

Cette capture a d'ailleurs immediatement montre un ecart qu'aucun test
fabrique n'aurait revele — cf. `test_le_bot_est_reconnu_sous_sa_forme_graphql`.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from reviewer.forge.base import ForgeError
from reviewer.forge.reader import GitHubReader, _checks, _erreurs, _threads
from reviewer.rules.machine import State, decide, normalise_login

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "pr400.json").read_text(encoding="utf-8")
)
PR = FIXTURE["data"]["repository"]["pullRequest"]
ROLLUP = PR["commits"]["nodes"][0]["commit"]["statusCheckRollup"]


# ── Checks ──────────────────────────────────────────────────────────────────


def test_les_checks_reels_se_lisent():
    checks, _ = _checks(ROLLUP)
    assert len(checks) == 11
    noms = {c.name for c in checks}
    assert "Test and build" in noms
    assert "branch-policy / check-branch-policy" in noms


def test_seul_un_check_reel_reste_apres_exclusion_de_notre_machinerie():
    # Sur cette PR, 10 des 11 checks sont de la machinerie de livraison
    # (claude, branch-policy, dependency-gate, hotfix-backmerge, release). Les
    # compter reviendrait a juger le processus au lieu du code livre.
    checks, _ = _checks(ROLLUP)
    reels = [c for c in checks if not c.ours]
    assert [c.name for c in reels] == ["Test and build"]


def test_les_skipped_de_notre_machinerie_ne_font_pas_echouer():
    checks, _ = _checks(ROLLUP)
    assert not any(c.failed for c in checks)


def test_l_instant_de_conclusion_est_celui_du_dernier_check_qui_compte():
    _, conclus = _checks(ROLLUP)
    assert conclus is not None
    # La fenetre de revue part de la : attendre la fin d'un `release` saute
    # n'aurait aucun sens, et compter depuis l'ouverture de la PR ferait
    # expirer la fenetre avant meme que la CI ait fini.
    assert conclus.tzinfo is not None


def test_un_rollup_absent_ne_casse_pas():
    # Une PR sans aucun check (depot sans CI) doit se lire, pas exploser.
    assert _checks(None) == ((), None)


# ── Fils de revue ───────────────────────────────────────────────────────────


def test_les_fils_reels_se_lisent():
    fils = _threads(PR["reviewThreads"])
    assert len(fils) == 2
    assert all(f.resolved for f in fils)
    assert {f.path for f in fils} == {
        "scripts/design-debt-hook.test.mjs", ".github/workflows/project-sync.yml"
    }


def test_un_fil_sans_ligne_retombe_sur_son_ancrage_d_origine():
    # GitHub met `line` a null quand le fil devient obsolete, mais garde
    # `originalLine`. Sans ce repli, la remarque perd sa position et le journal
    # ne dit plus ou elle porte.
    brut = PR["reviewThreads"]["nodes"]
    sans_ligne = [t for t in brut if t["comments"]["nodes"][0]["line"] is None]
    assert sans_ligne, "le fixture doit contenir un fil sans `line`"

    fils = _threads({"nodes": sans_ligne})
    assert fils[0].line == sans_ligne[0]["comments"]["nodes"][0]["originalLine"]


def test_le_bot_est_reconnu_sous_sa_forme_graphql():
    # LE PIEGE. GraphQL rend `chatgpt-codex-connector`, REST rend
    # `chatgpt-codex-connector[bot]`. Une allowlist ecrite dans l'une des deux
    # formes ne reconnait rien de ce que rend l'autre — et la panne est MUETTE :
    # zero fil retenu se lit exactement comme zero fil ouvert.
    fils = _threads(PR["reviewThreads"])
    auteurs = {f.author for f in fils}
    assert auteurs == {"chatgpt-codex-connector"}          # sans suffixe, cote GraphQL

    allowlist_ecrite_en_rest = frozenset({"chatgpt-codex-connector[bot]"})
    assert normalise_login("chatgpt-codex-connector") in {
        normalise_login(a) for a in allowlist_ecrite_en_rest
    }


def test_le_travail_est_bien_vu_avec_une_allowlist_ecrite_en_rest():
    # Le meme piege, mais vu de bout en bout : une remarque NON resolue d'un bot
    # rendu par GraphQL doit declencher le travail, alors que l'allowlist du
    # profil est ecrite avec le suffixe `[bot]`.
    brut = PR["reviewThreads"]["nodes"]
    ouvert = json.loads(json.dumps(brut[:1]))
    ouvert[0]["isResolved"] = False

    from reviewer.rules.machine import Check, PullSnapshot

    snap = PullSnapshot(
        number=400, repo="frontend", head_sha="x",
        checks=(Check("Test and build", "completed", "success"),),
        threads=_threads({"nodes": ouvert}),
    )
    d = decide(snap, trusted_reviewers={"chatgpt-codex-connector[bot]"})
    assert d.state is State.NEEDS_FIX


def test_un_fil_sans_commentaire_est_ignore():
    assert _threads({"nodes": [{"id": "x", "isResolved": False, "comments": {"nodes": []}}]}) == ()


def test_des_fils_absents_ne_cassent_pas():
    assert _threads(None) == ()


# ── Contextes de statut externes (Snyk) ─────────────────────────────────────


@pytest.mark.parametrize(("etat", "attendu_pending", "attendu_failed"), [
    ("SUCCESS", False, False),
    ("FAILURE", False, True),
    ("ERROR", False, True),
    ("PENDING", True, False),
    ("EXPECTED", True, False),
])
def test_un_statut_externe_se_traduit_dans_le_vocabulaire_des_checks(
    etat, attendu_pending, attendu_failed
):
    # `statusCheckRollup` melange `CheckRun` (Actions) et `StatusContext`
    # (services externes — Snyk chez Plantifia). N'en lire qu'un donne un « tout
    # est vert » qui ignore la moitie des signaux.
    checks, _ = _checks({"contexts": {"nodes": [
        {"__typename": "StatusContext", "context": "security/snyk",
         "state": etat, "createdAt": "2026-08-25T10:00:00Z"},
    ]}})
    assert len(checks) == 1
    assert checks[0].name == "security/snyk"
    assert checks[0].pending is attendu_pending
    assert checks[0].failed is attendu_failed


def test_un_type_inconnu_est_ignore_sans_casser():
    checks, _ = _checks({"contexts": {"nodes": [
        {"__typename": "QuelqueChoseDeNouveau", "machin": 1},
        {"__typename": "CheckRun", "name": "Test", "status": "COMPLETED",
         "conclusion": "SUCCESS", "completedAt": "2026-08-25T10:00:00Z"},
    ]}})
    assert [c.name for c in checks] == ["Test"]


# ── Transport ───────────────────────────────────────────────────────────────


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_open_pulls_construit_des_instantanes():
    charge = {"data": {"repository": {"pullRequests": {"nodes": [
        {**PR, "commits": PR["commits"], "reviewThreads": PR["reviewThreads"]}
    ]}}}}

    async with GitHubReader("Insectorize", "jeton",
                            client=_client(lambda r: httpx.Response(200, json=charge))) as g:
        pulls = await g.open_pulls("frontend")

    assert len(pulls) == 1
    assert pulls[0].number == 400
    assert pulls[0].repo == "frontend"
    assert len(pulls[0].checks) == 11
    assert len(pulls[0].threads) == 2


async def test_une_erreur_graphql_leve_meme_avec_un_statut_200():
    # GraphQL rend 200 MEME en erreur. Se fier au seul statut HTTP ferait lire
    # une reponse vide comme « ce depot n'a aucune PR ».
    charge = {"errors": [{"message": "Could not resolve to a Repository"}]}
    async with GitHubReader("Insectorize", "jeton",
                            client=_client(lambda r: httpx.Response(200, json=charge))) as g:
        with pytest.raises(ForgeError, match="Could not resolve"):
            await g.open_pulls("inexistant")


async def test_un_depot_invisible_est_dit_explicitement():
    charge = {"data": {"repository": None}}
    async with GitHubReader("Insectorize", "jeton",
                            client=_client(lambda r: httpx.Response(200, json=charge))) as g:
        with pytest.raises(ForgeError, match="introuvable ou invisible"):
            await g.open_pulls("prive")


async def test_un_statut_http_en_erreur_est_dit():
    async with GitHubReader("Insectorize", "jeton",
                            client=_client(lambda r: httpx.Response(401, text="Bad credentials"))) as g:
        with pytest.raises(ForgeError, match="401"):
            await g.open_pulls("frontend")


async def test_le_user_agent_est_envoye():
    # GitHub REFUSE les requetes sans User-Agent, avec un 403 sans message utile.
    vus: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        vus.update(request.headers)
        return httpx.Response(200, json={"data": {"repository": {"pullRequests": {"nodes": []}}}})

    async with GitHubReader("Insectorize", "jeton", client=_client(handler)) as g:
        await g.open_pulls("frontend")
    assert vus.get("user-agent") == "claude-agent-runner"
    assert vus.get("authorization") == "Bearer jeton"


# ── Une erreur de permission doit dire QUOI corriger ────────────────────────


def test_l_erreur_graphql_nomme_le_champ_refuse():
    # GitHub renvoie le meme message pour une dizaine de permissions. Sans le
    # `path`, il faut deviner laquelle manque — et on la devine mal.
    msg = _erreurs([{
        "message": "Resource not accessible by personal access token",
        "path": ["repository", "pullRequests", "nodes", 0, "commits", "nodes",
                 0, "commit", "statusCheckRollup"],
    }])
    assert "statusCheckRollup" in msg
    assert "Checks: Read" in msg


def test_l_erreur_graphql_traduit_les_fils_de_review():
    msg = _erreurs([{
        "message": "Resource not accessible by personal access token",
        "path": ["repository", "pullRequests", "nodes", 0, "reviewThreads"],
    }])
    assert "Pull requests: Read" in msg


def test_une_erreur_sans_chemin_reste_lisible():
    # Toutes les erreurs GraphQL ne portent pas de `path` (erreur de syntaxe,
    # variable manquante). Le message brut doit passer tel quel, sans decor.
    msg = _erreurs([{"message": "Bad credentials"}])
    assert msg == "Bad credentials"


# ── Repli sur l'API Actions quand `Checks` est refuse ───────────────────────
# Les jetons fine-grained n'offrent pas la permission `Checks` (constate le
# 25/08/2026). Perdre le depot entier pour ca serait absurde : le reste de la
# reponse GraphQL est bon, et l'etat de la CI se relit par l'API Actions.

_REFUS_CHECKS = {
    "message": "Resource not accessible by personal access token",
    "path": ["repository", "pullRequests", "nodes", 0, "commits", "nodes", 0,
             "commit", "statusCheckRollup"],
}


def _reponse_partielle():
    """PR et fils presents, `statusCheckRollup` refuse — la forme reelle."""
    pr = {**PR, "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}}
    return {"data": {"repository": {"pullRequests": {"nodes": [pr]}}},
            "errors": [_REFUS_CHECKS]}


async def test_un_refus_sur_les_checks_ne_perd_pas_la_pr():
    runs = {"workflow_runs": [
        {"name": "backend-ci", "status": "completed", "conclusion": "success",
         "updated_at": "2026-08-25T10:00:00Z"},
        {"name": "quality-gate", "status": "completed", "conclusion": "failure",
         "updated_at": "2026-08-25T10:05:00Z"},
    ]}

    def handler(request):
        if "graphql" in str(request.url):
            return httpx.Response(200, json=_reponse_partielle())
        return httpx.Response(200, json=runs)

    async with GitHubReader("Insectorize", "jeton", client=_client(handler)) as g:
        pulls = await g.open_pulls("backend")

    assert len(pulls) == 1, "la PR ne doit pas etre perdue"
    assert pulls[0].checks_readable is True
    assert {c.name for c in pulls[0].checks} == {"backend-ci", "quality-gate"}
    assert [c.name for c in pulls[0].checks if c.failed] == ["quality-gate"]


async def test_si_le_repli_echoue_la_ci_est_declaree_illisible():
    # Le point dangereux : rendre une liste vide dirait « aucun check », donc
    # « rien de rouge ». Il faut le DIRE.
    def handler(request):
        if "graphql" in str(request.url):
            return httpx.Response(200, json=_reponse_partielle())
        return httpx.Response(403, json={"message": "Forbidden"})

    async with GitHubReader("Insectorize", "jeton", client=_client(handler)) as g:
        pulls = await g.open_pulls("backend")

    assert len(pulls) == 1
    assert pulls[0].checks_readable is False
    assert pulls[0].checks == ()


async def test_une_erreur_hors_checks_reste_bloquante():
    # On ne tolere QUE le refus sur les checks. Une erreur ailleurs veut dire
    # qu'on ignore ce qui manque — continuer serait deviner.
    charge = {"data": {"repository": None},
              "errors": [_REFUS_CHECKS,
                         {"message": "Resource not accessible by personal access token",
                          "path": ["repository", "pullRequests"]}]}
    async with GitHubReader("Insectorize", "jeton",
                            client=_client(lambda r: httpx.Response(200, json=charge))) as g:
        with pytest.raises(ForgeError) as e:
            await g.open_pulls("backend")
    assert "Pull requests: Read" in str(e.value)
