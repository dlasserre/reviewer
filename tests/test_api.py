"""L'API locale rend-elle ce que le front V2 attendra ?

Le front n'existe pas encore. C'est precisement pour ca que ces tests existent :
la frontiere se fige maintenant, pendant qu'elle coute cent lignes, plutot
qu'apres coup quand il faudra rendre un journal retro-compatible.
"""

from __future__ import annotations

import textwrap
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from reviewer.output.api import create_app
from reviewer.config import load_profile, load_runner
from reviewer.output.events import Event, Journal
from reviewer.store.leases import PullState, StateStore

RUNNER = """
worktrees_root: {tmp}/wt
state_db: {tmp}/state.db
logs_dir: {tmp}/logs
"""

PROFIL = """
project: demo
workspace: {tmp}/demo
forge:
  org: UneOrg
repos:
  api:
    access: write
    path: "{{workspace}}/api"
  perso:
    access: context
    path: "{{workspace}}/perso"
"""


@pytest.fixture
def app_ctx(tmp_path):
    t = str(tmp_path).replace("\\", "/")
    (tmp_path / "runner.yaml").write_text(textwrap.dedent(RUNNER.format(tmp=t)), encoding="utf-8")
    (tmp_path / "p.yaml").write_text(textwrap.dedent(PROFIL.format(tmp=t)), encoding="utf-8")

    runner = load_runner(tmp_path / "runner.yaml")
    profil = load_profile(tmp_path / "p.yaml")
    store = StateStore(runner.state_db)
    journal = Journal(runner.logs_dir, profile="demo")
    # Battement court : le test ne doit pas attendre l'intervalle de production.
    app = create_app(runner, {"demo": profil}, store, journal, sse_keepalive_s=0.2)
    with TestClient(app) as client:
        yield client, store, journal
    store.close()


def test_health_dit_si_le_demon_peut_ecrire(app_ctx):
    client, _, _ = app_ctx
    r = client.get("/health")
    assert r.status_code == 200
    # C'est la premiere question devant un comportement inattendu.
    assert r.json()["writes_enabled"] is False
    assert r.json()["profiles"] == ["demo"]


def test_les_profils_disent_ce_qui_est_ecrivable(app_ctx):
    client, _, _ = app_ctx
    p = client.get("/profiles").json()[0]
    assert p["project"] == "demo"
    # Le seul chiffre qui dit ce que le profil autorise reellement.
    assert p["writable"] == ["api"]
    assert p["repos"]["perso"]["access"] == "context"


def test_jobs_est_vide_quand_rien_ne_tourne(app_ctx):
    client, _, _ = app_ctx
    assert client.get("/jobs").json()["active"] == []


def test_un_bail_pris_apparait_dans_jobs(app_ctx):
    client, store, _ = app_ctx
    store.acquire("demo", "api", 42, "j_test", ttl=timedelta(minutes=30))
    actifs = client.get("/jobs").json()["active"]
    assert len(actifs) == 1
    assert (actifs[0]["repo"], actifs[0]["pr"], actifs[0]["job_id"]) == ("api", 42, "j_test")


def test_un_bail_relache_disparait(app_ctx):
    client, store, _ = app_ctx
    b = store.acquire("demo", "api", 42, "j_test")
    store.release(b)
    assert client.get("/jobs").json()["active"] == []


def test_les_evenements_recents_sont_rendus(app_ctx):
    client, _, journal = app_ctx
    journal.emit(Event(event="reconcile.decision", profile="demo", repository="api",
                       pull_request=42, state="NEEDS_FIX", why="1 fil ouvert."))
    recents = client.get("/jobs").json()["recent"]
    assert recents[-1]["why"] == "1 fil ouvert."
    assert recents[-1]["state"] == "NEEDS_FIX"


def test_le_detail_d_une_pr_filtre_ses_evenements(app_ctx):
    client, store, journal = app_ctx
    journal.emit(Event(event="x", profile="demo", repository="api", pull_request=42,
                       why="pour la 42"))
    journal.emit(Event(event="x", profile="demo", repository="api", pull_request=43,
                       why="pour la 43"))
    store.save_pull_state(PullState("demo", "api", 42, claude_session="sess-1",
                                    review_cycle=2))

    d = client.get("/jobs/demo/api/42").json()
    assert d["state"]["claude_session"] == "sess-1"
    assert d["state"]["review_cycle"] == 2
    assert [e["why"] for e in d["events"]] == ["pour la 42"]


def test_une_pr_jamais_vue_rend_un_etat_neutre(app_ctx):
    client, _, _ = app_ctx
    d = client.get("/jobs/demo/api/999").json()
    assert d["lease"] is None
    assert d["state"]["review_cycle"] == 0
    assert d["events"] == []


def test_un_profil_inconnu_rend_404(app_ctx):
    client, _, _ = app_ctx
    assert client.get("/jobs/inexistant/api/1").status_code == 404


def test_le_flux_rejoue_les_evenements_recents(app_ctx):
    client, _, journal = app_ctx
    journal.emit(Event(event="reconcile.done", profile="demo", why="passage a vide"))

    # Sans rejeu, un front qui se connecte affiche un ecran vide jusqu'a la
    # prochaine transition — qui peut ne jamais venir.
    with client.stream("GET", "/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for ligne in r.iter_lines():
            if ligne.startswith("data: "):
                assert "passage a vide" in ligne
                break


# ── La console locale ───────────────────────────────────────────────────────


def test_la_console_est_servie_a_la_racine(app_ctx):
    client, _, _ = app_ctx
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Agent runner" in r.text


def test_la_console_ne_depend_d_AUCUN_reseau_externe(app_ctx):
    # Le demon doit pouvoir tourner sur une machine hors ligne. Une console qui
    # exige un CDN pour s'afficher serait inutilisable precisement quand on en
    # a besoin — et chaque hote externe est aussi une fuite de ce qu'on regarde.
    client, _, _ = app_ctx
    page = client.get("/").text
    # Ce qui compte n'est pas l'absence de « // » — les commentaires JS en
    # portent — mais l'absence de RESSOURCE chargee depuis ailleurs.
    for motif in ('src="http', "src='http", 'href="http', "href='http",
                  "@import", "cdn.", "unpkg", "jsdelivr", "googleapis"):
        assert motif not in page, f"dependance externe : {motif}"


def test_la_console_n_expose_AUCUNE_ecriture(app_ctx):
    # Le YAML est la frontiere de securite : il se relit, se versionne et se
    # compare en revue. Un formulaire qui le reecrirait sortirait les droits de
    # l'agent de tout ca.
    client, _, _ = app_ctx
    page = client.get("/").text
    for interdit in ("method=\"post\"", "<form", "fetch(\"/", "method: 'POST'",
                     "method: \"POST\""):
        assert interdit.lower() not in page.lower(), f"surface d'ecriture : {interdit}"


def test_la_sante_dit_l_armement_ET_le_parallelisme(app_ctx):
    # Les deux premieres questions devant un comportement inattendu : est-ce
    # qu'il a le droit d'ecrire, et combien de jobs peuvent se marcher dessus.
    client, _, _ = app_ctx
    d = client.get("/health").json()
    assert "writes_enabled" in d
    assert d["max_parallel"] >= 1


def test_les_profils_rendent_le_moteur_RESOLU_pas_la_table_brute(app_ctx):
    # Une entree qui ne nomme que l'effort retombe sur le modele global.
    # Afficher le fichier tel quel laisserait croire qu'aucun modele ne
    # s'applique a cette severite.
    client, _, _ = app_ctx
    p = client.get("/profiles").json()[0]
    assert set(p["engine"]) == {"P1", "P2", "P3", "UNKNOWN"}
    for reglage in p["engine"].values():
        assert set(reglage) == {"model", "effort"}
