"""L'orchestrateur relache-t-il toujours le bail, et refuse-t-il au bon moment ?

L'agent est REMPLACE par un double ici — c'est la seule piece qu'on ne peut pas
faire tourner en test. Tout le reste est reel : depot git, worktree, jonctions,
verifications, commit. Ce qui compte dans ce module n'est pas que le chemin
nominal marche, c'est que chaque sortie prematuree rende la main proprement.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewer.config import IssuesConfig, load_profile, load_runner
from reviewer.output.events import Journal
from reviewer.forge.base import ForgeError
from reviewer.agent.run import AgentOutcome
from reviewer.graph.build import construire, lancer_job
from reviewer.graph.deps import Deps
from reviewer.graph.nodes import _derivation
from reviewer.graph.sweep import Outcome
from reviewer.output.events import new_job_id
from reviewer.repo.worktree import WorktreeManager
from reviewer.rules.machine import (Action, Decision, PullSnapshot, State,
                                           Thread, decide)
from reviewer.store.leases import StateStore

PY = sys.executable


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, f"git {' '.join(args)} :\n{r.stderr}"
    return r.stdout


@pytest.fixture
def atelier(tmp_path):
    """Un depot reel + un profil + un runner, tous branches ensemble."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=dev", ".")

    depot = tmp_path / "ws" / "backend"
    depot.parent.mkdir(parents=True)
    git(tmp_path / "ws", "clone", str(origin), "backend")
    git(depot, "config", "user.email", "t@t.test")
    git(depot, "config", "user.name", "T")
    (depot / "app").mkdir()
    (depot / "app" / "service.py").write_text("x = 1\n", encoding="utf-8")
    git(depot, "add", "-A")
    git(depot, "commit", "-m", "initial")
    git(depot, "push", "-u", "origin", "dev")
    git(depot, "branch", "fix/714-truc")

    t = str(tmp_path).replace("\\", "/")
    (tmp_path / "runner.yaml").write_text(textwrap.dedent(f"""
        worktrees_root: {t}/agent/wt
        state_db: {t}/agent/state.db
        logs_dir: {t}/agent/logs
        writes_enabled: true
    """), encoding="utf-8")
    (tmp_path / "p.yaml").write_text(textwrap.dedent(f"""
        project: essai
        workspace: {t}/ws
        forge:
          org: UneOrg
        max_review_cycles: 3
        reviewers:
          trust: [chatgpt-codex-connector]
        repos:
          backend:
            access: write
            path: {t}/ws/backend
            checks: [ '"{PY}" -c "print(1)"' ]
    """), encoding="utf-8")

    runner = load_runner(tmp_path / "runner.yaml")
    profil = load_profile(tmp_path / "p.yaml")
    store = StateStore(runner.state_db)
    journal = Journal(runner.logs_dir, profile="essai")
    wt = WorktreeManager(root=runner.worktrees_root, profile="essai")
    yield depot, runner, profil, store, journal, wt
    store.close()


def travail(threads=None) -> Outcome:
    # Les fils sont portes par le SNAPSHOT, pas seulement par la decision : le
    # graphe relit la PR et redecide, donc il ne voit que ce que la forge rend.
    # Les laisser sur la seule decision faisait decider `READY_FOR_HUMAN` sur une
    # PR qui a pourtant un fil ouvert — et tous les tests d'agent seraient passes
    # a cote sans qu'aucun ne tombe.
    fils = threads if threads is not None else (
        Thread("PRRT_1", 1, "chatgpt-codex-connector", False, "**![P1 Badge](x)** truc",
               "app/service.py", 3),
    )
    snap = PullSnapshot(number=714, repo="backend", head_sha="abc",
                        head_ref="fix/714-truc", threads=fils)
    return Outcome("backend", 714,
                   Decision(State.NEEDS_FIX, "1 fil ouvert.", (Action.RUN_AGENT,),
                            threads=fils, consumes_cycle=True),
                   snap)


_FIL_DU_PROMPT = re.compile(r"_Fil `([^`]+)`")


def agent_qui(ecrit: str | None = None, *, session="sess-1", echoue=False,
              issue="corrige", reply="fait", blocked="", structure="auto"):
    """Double d'agent : il ecrit un fichier, ou echoue — et rend un VERDICT.

    Le verdict n'est pas un decor de test : depuis le 27/08/2026 c'est la seule
    chose que l'agent rend, et le runner n'agit que d'apres lui. Un double qui
    ne le rendrait pas testerait un agent qui n'existe pas.

    Les identifiants de fil sont relus DANS LE PROMPT, comme le fait le vrai
    agent. Les fabriquer depuis la fixture masquerait le cas ou le prompt ne
    porte pas l'identifiant qu'on croit — precisement ce qu'on veut verifier.
    """
    async def faux(prompt_text, *, worktree, profile, runner, guard,
                   resume=None, timeout_s=0, output_format=None, extra_dirs=(),
                   model=None, effort=None, on_step=None):
        # Le double IMITE le SDK : il annonce quelques etapes, comme le ferait
        # un vrai agent. Sans ca, rien ne verifierait que le runner les
        # journalise — et le fil temps reel de la console serait vide en
        # production sans qu'aucun test ne bronche.
        if on_step is not None:
            on_step("outil", "Read app/service.py")
            on_step("texte", "je regarde le service")
        faux.vu = {"prompt": prompt_text, "resume": resume, "worktree": worktree,
                   "output_format": output_format, "extra_dirs": extra_dirs,
                   "model": model, "effort": effort}
        if echoue:
            return AgentOutcome(session, "error_max_turns", 0.1, error="a cale")
        if ecrit is not None:
            (Path(worktree) / "app" / "service.py").write_text(ecrit, encoding="utf-8")
        if structure == "auto":
            rendu = {
                "summary": "corrige le court-circuit",
                "threads": [{"thread_id": tid, "outcome": issue, "reply": reply}
                            for tid in _FIL_DU_PROMPT.findall(prompt_text)],
                "blocked": blocked,
            }
        else:
            rendu = structure
        return AgentOutcome(session, "success", 0.42, "fait", structured=rendu)
    faux.vu = {}
    return faux


class FauxLecteur:
    """Rend les PR qu'on lui a donnees a servir.

    Le graphe commence par RELIRE la PR : sans lecteur, il n'y a rien a decider.
    Ce double sert exactement le `PullSnapshot` de la fixture, ce qui fait que
    les tests decrivent une SITUATION plutot qu'une decision.
    """

    def __init__(self) -> None:
        self.pulls: dict[tuple[str, int], PullSnapshot] = {}
        self.appels: list[str] = []

    def servir(self, snap: PullSnapshot) -> None:
        self.pulls[(snap.repo, snap.number)] = snap

    async def open_pulls(self, repo: str) -> list[PullSnapshot]:
        self.appels.append(repo)
        return [s for (r, _), s in self.pulls.items() if r == repo]


class Resultat:
    """Traduit l'etat final du graphe dans le vocabulaire des tests.

    Les tests d'origine lisaient un `JobResult`. Les memes noms pointent ici
    vers les champs de `JobState` — la correspondance est directe, un champ pour
    un champ, sans calcul : si elle demandait de deduire quoi que ce soit, c'est
    que l'etat du graphe aurait perdu une information.
    """

    def __init__(self, final: dict) -> None:
        self._f = final

    def __repr__(self) -> str:  # pragma: no cover — confort de debogage
        return f"Resultat({self.state} : {self.reason})"

    state = property(lambda s: s._f.get("next_state"))
    reason = property(lambda s: s._f.get("reason", ""))
    dry_run = property(lambda s: bool(s._f.get("dry_run")))
    branch = property(lambda s: s._f.get("branch"))
    derived = property(lambda s: bool(s._f.get("derived")))
    issue = property(lambda s: s._f.get("issue"))
    opened_pull = property(lambda s: s._f.get("opened_pull"))
    pushed = property(lambda s: bool(s._f.get("pushed")))
    replied = property(lambda s: s._f.get("replied", 0))
    resolved = property(lambda s: s._f.get("resolved", 0))
    asked = property(lambda s: s._f.get("asked", 0))
    diff = property(lambda s: s._f.get("diff"))
    checks = property(lambda s: s._f.get("checks"))
    session_id = property(
        lambda s: s._f["agent"].session_id if s._f.get("agent") else None)

    @property
    def worktree(self):
        chemin = self._f.get("worktree")
        return Path(chemin) if chemin else None


class Lanceur:
    """Fait tourner le GRAPHE sur une PR, et rend le resultat.

    ── CE QUE LES FIXTURES NE PILOTENT PLUS ────────────────────────────────

    `travail()` et `travail_release()` rendent un `Outcome`, donc une
    `Decision`. Le JobRunner la CONSOMMAIT. Le graphe, lui, part de `observe` :
    il relit la PR et RECALCULE la decision depuis le snapshot.

    Cette `Decision` de fixture n'est donc plus un pilote, c'est une INTENTION —
    « voila ce qu'on attend de `decide` sur cette situation ». Elle n'est pas
    devenue decorative pour autant : `test_les_fixtures_disent_vrai` verifie que
    `decide` produit bien ce que la fixture annonce. Sans ce test, une fixture
    pourrait derailler en silence et les cinquante autres tests continueraient
    de passer en testant autre chose que ce qu'ils annoncent.
    """

    def __init__(self, deps: Deps, graphe, lecteur: FauxLecteur) -> None:
        self.deps = deps
        self.graphe = graphe
        self.lecteur = lecteur

    async def run(self, outcome: Outcome, *, repo_path, write_token=None):
        self.lecteur.servir(outcome.snapshot)
        final = await lancer_job(
            self.graphe,
            project=self.deps.profile.project,
            repo=outcome.repo, pr=outcome.number,
            job_id=new_job_id(), repo_path=str(repo_path),
            write_token=write_token,
            forced=outcome.forced,
        )
        return Resultat(final)


def graphe_de(atelier, agent, writer=None):
    """Le harnais complet : dependances, graphe, lanceur."""
    depot, runner, profil, store, journal, wt = atelier
    lecteur = FauxLecteur()
    deps = Deps(runner=runner, profile=profil, store=store, journal=journal,
                worktrees=wt, reader=lecteur, writer=writer, agent=agent)
    return Lanceur(deps, construire(deps), lecteur), depot, store


def runner_de(atelier, agent):
    return graphe_de(atelier, agent)


async def test_les_fixtures_disent_vrai(atelier):
    """`decide` produit-il bien la decision que les fixtures annoncent ?

    Le pivot du portage. Les fixtures ne pilotent plus la decision, elles la
    DECRIVENT : si `decide` cessait de rendre `NEEDS_FIX` sur ces situations,
    tous les tests de ce fichier passeraient encore — en testant un chemin qui
    n'existe pas. Ce test est le seul qui s'en apercevrait.
    """
    _, _, profil, _, _, _ = atelier
    trust = frozenset(profil.reviewers.trust)
    for fixture in (travail(), travail_release()):
        d = decide(fixture.snapshot, trusted_reviewers=trust,
                   max_review_cycles=profil.max_review_cycles)
        assert d.state is fixture.decision.state, fixture.snapshot.number
        assert Action.RUN_AGENT in d.actions, fixture.snapshot.number
        assert [t.id for t in d.threads] == [t.id for t in fixture.decision.threads]


# ── L'environnement donne a l'agent ─────────────────────────────────────────


def test_l_agent_recoit_l_outillage_du_DEPOT(atelier, tmp_path):
    # Mesure du 27/08/2026, premier job reel : l'agent a lu le code, corrige
    # trois fichiers, puis passe les tours 55 a 66 a chercher un interpreteur
    # Python — `where.exe python`, `printenv VIRTUAL_ENV`, exploration de
    # repertoires. Il a epuise `max_turns` sur cette recherche, et le cycle a
    # ete perdu alors que le correctif etait ecrit.
    #
    # Les verifications du runner preparaient deja leur PATH ; l'agent, non.
    # On ne peut pas lui demander de « lancer les tests du depot » sans lui
    # donner de quoi les lancer.
    from reviewer.agent.run import agent_env

    _, runner, _, _, _, _ = atelier
    faux_venv = tmp_path / "wt" / ".venv" / "Scripts"
    faux_venv.mkdir(parents=True)

    env = agent_env(runner, tmp_path / "wt")

    assert env["PATH"].split(os.pathsep)[0] == str(faux_venv)
    assert env["VIRTUAL_ENV"] == str(faux_venv.parent)


def test_le_PATH_est_reconstruit_ENTIER_pas_tronque(atelier, tmp_path):
    # Ce dictionnaire ECRASE les variables de meme nom cote SDK : un PATH qui
    # ne contiendrait que le venv effacerait tout le reste de l'outillage.
    from reviewer.agent.run import agent_env

    _, runner, _, _, _, _ = atelier
    (tmp_path / "wt" / ".venv" / "bin").mkdir(parents=True)

    env = agent_env(runner, tmp_path / "wt")

    assert os.environ.get("PATH", "") in env["PATH"]


def test_sans_venv_le_PATH_n_est_PAS_touche(atelier, tmp_path):
    # Un depot sans venv (JavaScript, Go…) ne doit pas voir son PATH reecrit
    # pour rien : le reecrire a l'identique est une occasion de se tromper.
    from reviewer.agent.run import agent_env

    _, runner, _, _, _, _ = atelier
    (tmp_path / "vide").mkdir()
    assert "PATH" not in agent_env(runner, tmp_path / "vide")


def test_la_cle_d_API_reste_effacee_dans_l_environnement_de_l_agent(atelier, tmp_path):
    # `ANTHROPIC_API_KEY` prime sur `CLAUDE_CODE_OAUTH_TOKEN` : la laisser
    # basculerait la facturation de l'abonnement vers l'API, en silence. Ce
    # test garde la porte fermee pendant qu'on touche a cette fonction.
    from reviewer.agent.run import agent_env

    _, runner, _, _, _, _ = atelier
    (tmp_path / "wt" / ".venv" / "Scripts").mkdir(parents=True)
    assert agent_env(runner, tmp_path / "wt")["ANTHROPIC_API_KEY"] == ""


# ── Sorties prematurees ─────────────────────────────────────────────────────


async def test_sans_travail_demande_on_ne_fait_rien(atelier):
    agent = agent_qui("x = 2\n")
    jr, depot, _ = runner_de(atelier, agent)
    inerte = Outcome("backend", 714,
                     Decision(State.READY_FOR_HUMAN, "rien.", ()),
                     PullSnapshot(number=714, repo="backend", head_sha="a"))
    r = await jr.run(inerte, repo_path=depot)
    assert r.state is State.READY_FOR_HUMAN
    # La raison vient desormais de `decide`, qui NOMME ce qu'il constate. On
    # verifie donc ce qui compte vraiment : aucun agent n'a ete lance.
    assert "aucun fil ouvert" in r.reason
    assert agent.vu == {}, "l'agent ne doit PAS avoir ete appele"


async def test_un_bail_deja_pris_arrete_tout(atelier):
    # LE point d'idempotence : un webhook recu deux fois ne lance pas deux
    # agents.
    jr, depot, store = runner_de(atelier, agent_qui("x = 2\n"))
    store.acquire("essai", "backend", 714, "un-autre-job", ttl=timedelta(minutes=30))

    r = await jr.run(travail(), repo_path=depot)
    assert r.state is State.AGENT_WORKING
    assert "tient deja" in r.reason


async def test_l_heure_n_empeche_plus_de_travailler(atelier):
    # Une fenetre 07:00-23:00 a existe ici, retiree le 26/08/2026. Elle
    # eteignait l'agent la nuit : precisement quand personne ne dispute le
    # quota, et quand un demon cense absorber l'asynchronie sert le plus.
    # Ce test garde la porte fermee a son retour par accident.
    jr, depot, _ = runner_de(atelier, agent_qui("x = 2\n"))
    r = await jr.run(travail(), repo_path=depot)
    assert r.state is not State.IDLE
    assert "fenetre" not in r.reason


async def test_en_lecture_seule_le_prompt_est_construit_mais_l_agent_ne_tourne_pas(atelier):
    # `writes_enabled` est lu par l'aiguillage QUI SUIT `plan` : il faut donc le
    # poser avant de monter le graphe, pas apres.
    depot, runner, profil, store, journal, wt = atelier
    object.__setattr__(runner, "writes_enabled", False)
    agent = agent_qui("x = 2\n")
    jr, depot, store = graphe_de(atelier, agent)

    r = await jr.run(travail(), repo_path=depot)

    assert r.dry_run
    assert agent.vu == {}, "l'agent ne doit PAS avoir ete appele"
    assert not (runner.worktrees_root / "essai-backend-pr-714").exists()
    # Et le bail est relache : sinon la PR resterait bloquee pour rien.
    assert store.lease("essai", "backend", 714) is None


# ── Chemin nominal ──────────────────────────────────────────────────────────


async def test_le_travail_va_jusqu_au_commit(atelier):
    jr, depot, store = runner_de(atelier, agent_qui("x = 2  # corrige\n"))
    r = await jr.run(travail(), repo_path=depot)

    # Sans jeton, rien n'est pousse — donc AUCUNE CI ne va se declencher.
    # Annoncer `WAITING_CI` ici, ce que faisait la version d'avant le
    # 27/08/2026, decrivait une attente qui n'existait pas : le tableau montrait
    # une PR « en attente de CI » indefiniment.
    assert r.state is State.READY_FOR_HUMAN, r.reason
    assert "non pousse" in r.reason
    assert r.diff is not None and "app/service.py" in r.diff.files
    assert r.checks is not None and r.checks.ok
    assert not r.pushed, "sans jeton d'ecriture, on commite sans pousser"
    assert git(r.worktree, "log", "--oneline", "-1")


async def test_la_session_et_le_cycle_sont_enregistres(atelier):
    jr, depot, store = runner_de(atelier, agent_qui("x = 2\n", session="sess-abc"))
    await jr.run(travail(), repo_path=depot)

    etat = store.pull_state("essai", "backend", 714)
    assert etat.claude_session == "sess-abc"
    assert etat.review_cycle == 1
    # Le curseur avance : sans lui, la meme remarque relancerait un cycle.
    assert etat.last_handled_comment_id == 1


async def test_le_curseur_prend_le_plus_grand_identifiant(atelier):
    fils = tuple(
        Thread(f"PRRT_{i}", i, "chatgpt-codex-connector", False, "**P1** x", "a.py", 1)
        for i in (10, 42, 7)
    )
    jr, depot, store = runner_de(atelier, agent_qui("x = 2\n"))
    await jr.run(travail(fils), repo_path=depot)
    assert store.pull_state("essai", "backend", 714).last_handled_comment_id == 42


async def test_la_session_est_reprise_aux_premiers_cycles(atelier):
    depot, runner, profil, store, journal, wt = atelier
    store.save_pull_state(store.pull_state("essai", "backend", 714).__class__(
        "essai", "backend", 714, claude_session="sess-precedente", review_cycle=1))
    agent = agent_qui("x = 3\n")
    jr = graphe_de(atelier, agent)[0]

    await jr.run(travail(), repo_path=depot)
    assert agent.vu["resume"] == "sess-precedente"


async def test_au_dela_de_deux_cycles_on_repart_d_une_session_neuve(atelier):
    # Un contexte qui s'accumule coute de plus en plus cher a chaque cycle, et
    # le quota est partage.
    depot, runner, profil, store, journal, wt = atelier
    store.save_pull_state(store.pull_state("essai", "backend", 714).__class__(
        "essai", "backend", 714, claude_session="sess-longue", review_cycle=2))
    agent = agent_qui("x = 4\n")
    jr = graphe_de(atelier, agent)[0]

    await jr.run(travail(), repo_path=depot)
    assert agent.vu["resume"] is None


# ── Arrets propres ──────────────────────────────────────────────────────────


async def test_un_agent_qui_cale_rend_la_main(atelier):
    jr, depot, store = runner_de(atelier, agent_qui(echoue=True))
    r = await jr.run(travail(), repo_path=depot)
    assert r.state is State.NEEDS_HUMAN
    assert "n'a pas conclu" in r.reason
    assert store.lease("essai", "backend", 714) is None


async def test_un_correctif_annonce_mais_absent_rend_la_main(atelier):
    # L'agent affirme avoir corrige et l'arbre est propre. On ne publie pas une
    # affirmation qu'on sait fausse : la remarque serait comptee soldee, et le
    # fil referme, sur un correctif qui n'existe pas.
    jr, depot, store = runner_de(atelier, agent_qui(None, issue="corrige"))
    r = await jr.run(travail(), repo_path=depot)
    assert r.state is State.NEEDS_HUMAN
    assert "l'arbre est propre" in r.reason


async def test_un_desaccord_SANS_diff_n_est_pas_un_echec(atelier):
    # Le pendant du test precedent, et la vraie nouveaute : un cycle ou l'agent
    # a lu, juge la remarque fausse et n'a rien change est un resultat
    # LEGITIME. La version d'avant le 27/08/2026 le traitait comme une panne
    # (« l'agent n'a rien modifie »), ce qui transformait tout desaccord
    # argumente en echec.
    jr, depot, store = runner_de(atelier, agent_qui(None, issue="refute",
                                                    reply="le code fait deja X"))
    r = await jr.run(travail(), repo_path=depot)

    assert r.state is State.NEEDS_HUMAN, "un desaccord appelle l'humain"
    assert "l'arbre est propre" not in r.reason
    assert "0 corrige(s)" in r.reason
    # Le cycle est bien consomme, et le curseur avance : la meme remarque ne
    # doit pas relancer un cycle identique au passage suivant.
    etat = store.pull_state("essai", "backend", 714)
    assert etat.review_cycle == 1
    assert etat.last_handled_comment_id == 1


async def test_des_verifications_rouges_empechent_le_commit(atelier):
    depot, runner, profil, store, journal, wt = atelier
    rouge = f'"{PY}" -c "import sys; sys.exit(1)"'
    object.__setattr__(profil.repos["backend"], "checks", [rouge])
    jr = graphe_de(atelier, agent_qui("x = 2\n"))[0]

    r = await jr.run(travail(), repo_path=depot)

    assert r.state is State.NEEDS_HUMAN
    assert "rouges" in r.reason
    # Rien n'a ete commite : une PR verte en apparence et rouge en CI est un
    # aller-retour de plus.
    assert git(r.worktree, "status", "--porcelain").strip()


async def test_le_bail_est_relache_meme_quand_tout_explose(atelier):
    async def agent_qui_explose(*a, **k):
        raise RuntimeError("panne imprevue")

    jr, depot, store = runner_de(atelier, agent_qui_explose)
    r = await jr.run(travail(), repo_path=depot)

    assert r.state is State.NEEDS_HUMAN
    assert "RuntimeError" in r.reason
    # Un bail non relache bloquerait la PR jusqu'a son expiration, et une PR
    # bloquee sans motif visible est la signature qu'on evite.
    assert store.lease("essai", "backend", 714) is None


# ── Ce que l'agent recoit ───────────────────────────────────────────────────


async def test_l_agent_travaille_dans_SON_worktree(atelier):
    depot, runner, profil, store, journal, wt = atelier
    agent = agent_qui("x = 2\n")
    jr = graphe_de(atelier, agent)[0]
    await jr.run(travail(), repo_path=depot)

    utilise = Path(agent.vu["worktree"]).resolve()
    assert utilise == wt.path_for("backend", 714).resolve()
    assert utilise != depot.resolve(), "jamais la copie de travail humaine"


async def test_le_prompt_porte_la_remarque_et_le_cycle(atelier):
    depot, runner, profil, store, journal, wt = atelier
    agent = agent_qui("x = 2\n")
    jr = graphe_de(atelier, agent)[0]
    await jr.run(travail(), repo_path=depot)

    prompt = agent.vu["prompt"]
    assert "app/service.py:3" in prompt
    assert "Cycle de correction 1 sur 3" in prompt
    assert "DONNEES-EXTERNES" in prompt


async def test_le_schema_de_verdict_est_impose_a_l_agent(atelier):
    # Sans `output_format`, l'agent rend de la prose et le runner n'a plus rien
    # a exploiter : aucun fil ne serait repondu ni resolu. La panne serait
    # muette — un job « reussi » qui ne publie rien.
    depot, runner, profil, store, journal, wt = atelier
    agent = agent_qui("x = 2  # corrige\n")
    jr = graphe_de(atelier, agent)[0]
    await jr.run(travail(), repo_path=depot)

    impose = agent.vu["output_format"]
    assert impose is not None
    champs = impose["schema"]["properties"]["threads"]["items"]["properties"]
    assert set(champs) == {"thread_id", "outcome", "reply"}


# ── PR dont la TETE est une branche partagee (release, integration) ─────────


def travail_release(threads=None) -> Outcome:
    """Une PR de release : `dev` -> `main`. Sa tete est une branche PARTAGEE.

    C'est le cas reel qui rendait l'agent inutile : `backend#727` et
    `mobile#100`, le 27/08/2026. Le worktree refusait de se monter, le job
    s'arretait, et les deux seules PR ouvertes du projet n'etaient jamais
    traitees.
    """
    fils = threads if threads is not None else (
        Thread("PRRT_9", 9, "chatgpt-codex-connector", False,
               "**![P1 Badge](x)** mur qui casse le schema", "app/service.py", 229),
    )
    snap = PullSnapshot(number=727, repo="backend", head_sha="abc", head_ref="dev",
                        threads=fils)
    return Outcome("backend", 727,
                   Decision(State.NEEDS_FIX, "1 fil ouvert.", (Action.RUN_AGENT,),
                            threads=fils, consumes_cycle=True),
                   snap)


class FauxWriter:
    """Double du writer : enregistre ce qui aurait ete publie.

    Il n'imite pas GitHub, il enregistre des INTENTIONS — c'est tout ce que le
    runner decide. Ce que GitHub fait de ces appels est teste ailleurs, contre
    un transport HTTP double.
    """

    def __init__(self, *, issue_existante=None, issue_casse=False,
                 champs_casses=False):
        self.reponses, self.resolus, self.pulls, self.commentaires = [], [], [], []
        self.labels = []
        self.issues, self.types, self.priorites = [], [], []
        self._existante = issue_existante
        self._casse = issue_casse
        self._champs_casses = champs_casses

    async def find_issue_by_marker(self, repo, marker):
        return self._existante

    async def create_issue(self, repo, *, title, body, labels=(), assignee=None):
        if self._casse:
            raise ForgeError("403 sur la creation d'issue")
        self.issues.append({"repo": repo, "title": title, "body": body,
                            "labels": list(labels), "assignee": assignee})
        return {"number": 900 + len(self.issues),
                "html_url": f"https://github.com/UneOrg/{repo}/issues/901"}

    async def set_issue_type(self, repo, number, type_name):
        if self._champs_casses:
            raise ForgeError("403 : champs d'organisation inaccessibles")
        self.types.append((repo, number, type_name))

    async def set_issue_priority(self, repo, number, *, field_id, value):
        if self._champs_casses:
            raise ForgeError("403 : champs d'organisation inaccessibles")
        self.priorites.append((repo, number, field_id, value))

    async def reply_in_thread(self, thread_id, body, *, awaiting_human=False):
        self.reponses.append((thread_id, body, awaiting_human))
        return len(self.reponses)

    async def resolve_thread(self, thread_id):
        self.resolus.append(thread_id)

    async def comment_on_pull(self, repo, number, body, *, awaiting_human=False):
        self.commentaires.append((repo, number, body, awaiting_human))
        return len(self.commentaires)

    async def add_label(self, repo, number, label):
        self.labels.append((repo, number, label))

    async def create_pull(self, repo, *, head, base, title, body):
        self.pulls.append({"head": head, "base": base, "title": title, "body": body})
        return {"html_url": f"https://github.com/UneOrg/{repo}/pull/900",
                "head": {"ref": head}}


async def test_une_PR_de_release_ne_fait_PLUS_echouer_le_job(atelier):
    # LE test de la panne du 27/08/2026. Avant : « dev est une branche
    # partagee » -> NEEDS_HUMAN, rien de fait. Maintenant : on derive.
    jr, depot, store = runner_de(atelier, agent_qui("x = 2  # corrige\n"))
    r = await jr.run(travail_release(), repo_path=depot)

    assert r.state is not State.NEEDS_HUMAN, r.reason
    assert "branche partagee" not in r.reason
    assert r.derived is True
    assert r.branch == "fix/pr727-revue"


async def test_le_correctif_d_une_release_n_atterrit_JAMAIS_sur_dev(atelier):
    # La raison d'etre du refus d'origine, qui doit survivre a sa levee : le
    # worktree isole le repertoire, pas la reference.
    jr, depot, store = runner_de(atelier, agent_qui("x = 2  # corrige\n"))
    r = await jr.run(travail_release(), repo_path=depot)

    assert git(r.worktree, "branch", "--show-current").strip() == "fix/pr727-revue"
    # `dev` du depot principal n'a pas bouge : un seul commit, celui du fixture.
    assert len(git(depot, "log", "--oneline", "dev").strip().splitlines()) == 1


async def test_une_PR_derivee_est_ouverte_vers_la_branche_partagee(atelier):
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui("x = 2  # corrige\n"), writer=w)[0]

    r = await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    assert r.pushed
    assert len(w.pulls) == 1
    assert w.pulls[0]["head"] == "fix/pr727-revue"
    assert w.pulls[0]["base"] == "dev", "la PR derivee vise la branche relue"
    assert r.opened_pull == "https://github.com/UneOrg/backend/pull/900"


async def test_le_corps_de_la_PR_derivee_ne_declenche_PAS_la_projection(atelier):
    # « Fixes #<n> » deplacerait la carte de la PR de RELEASE. Le Worker de
    # projection lit ces mots-cles, et le numero cite serait le mauvais.
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui("x = 2  # corrige\n"), writer=w)[0]
    await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    corps = w.pulls[0]["body"].lower()
    for piege in ("fixes #", "closes #", "resolves #", "fix #"):
        assert piege not in corps, f"« {piege} » deplacerait la mauvaise carte"


async def test_l_agent_sait_que_son_travail_part_ailleurs(atelier):
    # Sans cette information, il ecrirait « corrige sur cette PR » dans une
    # reponse publiee sur une PR ou le correctif n'est pas.
    depot, runner, profil, store, journal, wt = atelier
    agent = agent_qui("x = 2  # corrige\n")
    jr = graphe_de(atelier, agent)[0]
    await jr.run(travail_release(), repo_path=depot)

    prompt = agent.vu["prompt"]
    assert "Ou atterrit ton travail" in prompt
    assert "fix/pr727-revue" in prompt


# ── Ce que le runner publie ─────────────────────────────────────────────────


async def test_un_fil_corrige_est_repondu_ET_resolu(atelier):
    # Le fil resolu est ce qui compte comme solde, pas la reponse : un correctif
    # pousse, argumente et vert dont le fil reste ouvert laisse la remarque
    # comptee comme ouverte — et retient le merge.
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui("x = 2  # ok\n"), writer=w)[0]

    r = await jr.run(travail(), repo_path=depot, write_token="jeton")

    assert [t for t, _, _ in w.reponses] == ["PRRT_1"]
    assert w.resolus == ["PRRT_1"]
    assert r.replied == 1 and r.resolved == 1 and r.asked == 0


async def test_un_arbitrage_est_repondu_mais_JAMAIS_resolu(atelier):
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui(None, issue="arbitrage",
                                   reply="deux corrections opposees se defendent"), writer=w)[0]

    r = await jr.run(travail(), repo_path=depot, write_token="jeton")

    assert w.resolus == [], "resoudre reviendrait a clore le debat en sa faveur"
    _, corps, attend = w.reponses[0]
    assert attend is True, "la reponse doit porter le marqueur d'attente"
    assert r.asked == 1


async def test_l_arbitrage_mentionne_l_humain_a_prevenir(atelier):
    depot, runner, profil, store, journal, wt = atelier
    object.__setattr__(profil.human, "notify", "@dlasserre")
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui(None, issue="arbitrage"), writer=w)[0]
    await jr.run(travail(), repo_path=depot, write_token="jeton")

    assert "@dlasserre" in w.reponses[0][1]


async def test_un_fil_corrige_ne_mentionne_PERSONNE(atelier):
    # Mentionner a chaque reponse apprend a ignorer la mention, et le jour ou
    # elle compte, elle ne compte plus.
    depot, runner, profil, store, journal, wt = atelier
    object.__setattr__(profil.human, "notify", "@dlasserre")
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui("x = 2  # ok\n"), writer=w)[0]
    await jr.run(travail(), repo_path=depot, write_token="jeton")

    assert "@dlasserre" not in w.reponses[0][1]


async def test_un_arret_previent_quelqu_un(atelier):
    # Un arret journalise dans un JSONL local est un arret que personne ne
    # verra. C'est le defaut qui faisait dire « l'agent ne fait rien » : il
    # faisait quelque chose, mais ne le disait qu'a lui-meme.
    depot, runner, profil, store, journal, wt = atelier
    object.__setattr__(profil.human, "notify", "@dlasserre")
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui(echoue=True), writer=w)[0]

    r = await jr.run(travail(), repo_path=depot, write_token="jeton")

    assert r.state is State.NEEDS_HUMAN
    assert len(w.commentaires) == 1
    assert "@dlasserre" in w.commentaires[0][2]
    assert w.commentaires[0][3] is True


async def test_un_arret_consomme_un_cycle(atelier):
    # Sans ca, un job qui echoue laisse l'etat inchange : meme decision au
    # passage suivant, meme echec, indefiniment. La boucle ne convergeait
    # jamais vers NEEDS_HUMAN, elle tournait.
    jr, depot, store = runner_de(atelier, agent_qui(echoue=True))
    await jr.run(travail(), repo_path=depot)
    assert store.pull_state("essai", "backend", 714).review_cycle == 1


async def test_le_meme_arret_ne_se_repete_pas_sur_la_meme_tete(atelier):
    # Un commentaire de PR n'a pas de marqueur d'attente pour le proteger : sans
    # l'ancrage sur le SHA, l'agent reposterait le meme arret a chaque passage.
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui(echoue=True), writer=w)[0]

    await jr.run(travail(), repo_path=depot, write_token="jeton")
    await jr.run(travail(), repo_path=depot, write_token="jeton")

    assert len(w.commentaires) == 1, "le second passage ne doit rien reposter"


# ── L'issue qui rattache le travail derive ──────────────────────────────────


def avec_issues(profil, **kw):
    """Active le rattachement a une issue sur le profil de l'atelier."""
    reglages = dict(enabled=True, type="Bug", priority_field=42021423,
                    assignee="dlasserre")
    reglages.update(kw)
    object.__setattr__(profil, "issues", IssuesConfig(**reglages))
    object.__setattr__(profil, "derived_branch", "fix/{issue}-revue-pr{pr}")
    object.__setattr__(profil.repos["backend"], "labels", ["backend"])
    return profil


def runner_issues(atelier, agent, writer, **kw):
    # `avec_issues` mute le profil : AVANT de construire les dependances, sinon
    # le graphe capturerait la version d'avant et n'ouvrirait aucune issue.
    avec_issues(atelier[2], **kw)
    return graphe_de(atelier, agent, writer=writer)


async def test_une_PR_derivee_ouvre_une_issue(atelier):
    w = FauxWriter()
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)

    r = await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    assert len(w.issues) == 1
    assert r.issue == 901
    assert w.issues[0]["labels"] == ["backend"], "la zone est demandee par le depot"
    assert w.issues[0]["assignee"] == "dlasserre"


async def test_l_issue_NOMME_la_branche_donc_existe_avant_le_worktree(atelier):
    # L'ordre n'est pas un detail : un nom de branche ne se corrige pas apres
    # coup sans reecrire une reference deja publiee.
    w = FauxWriter()
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)

    r = await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    assert r.branch == "fix/901-revue-pr727"
    assert git(r.worktree, "branch", "--show-current").strip() == "fix/901-revue-pr727"


async def test_la_PR_derivee_porte_Fixes_vers_SON_issue(atelier):
    # `Fixes #` est le mot-cle qui lie l'issue et fait avancer la carte. Il doit
    # viser l'issue que cette PR solde — jamais le numero de la PR relue, qui
    # ferait bouger la carte d'une livraison entiere.
    w = FauxWriter()
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)
    await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    corps = w.pulls[0]["body"]
    assert "Fixes #901" in corps
    assert "Fixes #727" not in corps


async def test_le_COMMIT_porte_Refs_et_jamais_Fixes(atelier):
    # GitHub ferme une issue des qu'un commit portant `Fixes #` atteint la
    # branche par defaut. Sur une issue qui s'acheve par une operation
    # (migration, backfill), elle se fermerait au deploiement du code — avant
    # que le geste soit fait — et le tableau annoncerait « termine » a tort.
    w = FauxWriter()
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)
    r = await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    message = git(r.worktree, "log", "-1", "--format=%B")
    assert "Refs #901" in message
    assert "Fixes #" not in message


async def test_le_type_et_la_priorite_sont_poses(atelier):
    w = FauxWriter()
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)
    await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    assert w.types == [("backend", 901, "Bug")]
    # La remarque de `travail_release` porte un badge P1 -> priorite haute.
    assert w.priorites == [("backend", 901, 42021423, "High")]


async def test_la_priorite_suit_la_remarque_la_PLUS_severe(atelier):
    # `Severity` est ordonnee du plus grave au moins grave, `UNKNOWN` valant 99 :
    # une remarque qu'on n'a pas su classer ne doit pas tirer la priorite vers
    # le bas.
    fils = (
        Thread("PRRT_1", 1, "chatgpt-codex-connector", False,
               "**![P3 Badge](x)** nommage", "a.py", 1),
        Thread("PRRT_2", 2, "chatgpt-codex-connector", False,
               "**![P2 Badge](x)** quota", "b.py", 2),
    )
    w = FauxWriter()
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)
    await jr.run(travail_release(fils), repo_path=depot, write_token="jeton")

    assert w.priorites[0][3] == "Medium"


async def test_des_champs_natifs_INACCESSIBLES_ne_tuent_pas_le_job(atelier):
    # Mesure du 27/08/2026 : un PAT fine-grained recoit 403 sur les champs
    # d'organisation. L'issue existe et elle est liee ; un champ de classement
    # se remplit a la main en deux secondes. Echouer ici couterait le correctif.
    w = FauxWriter(champs_casses=True)
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)

    r = await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    assert r.issue == 901
    assert r.state is not State.NEEDS_HUMAN, r.reason
    assert "Fixes #901" in w.pulls[0]["body"]


async def test_une_issue_IMPOSSIBLE_degrade_sans_perdre_le_correctif(atelier):
    w = FauxWriter(issue_casse=True)
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)

    r = await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    assert r.issue is None
    assert r.pushed, "le correctif part quand meme"
    # Nom de repli, volontairement DIFFERENT du modele nominal : il doit se
    # reconnaitre au premier coup d'oeil dans `git branch`.
    assert r.branch == "fix/pr727-revue"
    # Et aucun mot-cle de liaison, puisqu'il n'y a rien a lier.
    assert "Fixes #" not in w.pulls[0]["body"]


# ── Idempotence de la creation ─────────────────────────────────────────────


async def test_un_second_passage_REUTILISE_l_issue(atelier):
    # Le demon rejoue ses passages. Sans cle d'idempotence, chaque passage
    # ouvrirait une issue de plus sur le meme sujet.
    w = FauxWriter()
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)

    await jr.run(travail_release(), repo_path=depot, write_token="jeton")
    await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    assert len(w.issues) == 1, "une seule issue pour cette PR"
    assert store.pull_state("essai", "backend", 727).derived_issue == 901


async def test_l_issue_survit_a_la_PERTE_de_l_etat_local(atelier):
    # Filet : la base effacee, la machine changee. La recherche par marqueur
    # rattrape — c'est aussi la « recherche de doublons » que le depot demande
    # avant toute creation.
    w = FauxWriter(issue_existante={"number": 555, "body": "<!-- marqueur -->"})
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)

    r = await jr.run(travail_release(), repo_path=depot, write_token="jeton")

    assert w.issues == [], "aucune creation : on a retrouve l'existante"
    assert r.issue == 555
    assert store.pull_state("essai", "backend", 727).derived_issue == 555


async def test_une_PR_ORDINAIRE_n_ouvre_AUCUNE_issue(atelier):
    # Elle a deja la sienne : son correctif y retourne. En creer une seconde
    # dedoublerait le suivi.
    w = FauxWriter()
    jr, depot, store = runner_issues(atelier, agent_qui("x = 2  # ok\n"), w)

    r = await jr.run(travail(), repo_path=depot, write_token="jeton")

    assert w.issues == []
    assert r.issue is None
    assert r.branch == "fix/714-truc", "on travaille sur la tete de la PR"


async def test_le_mode_lecture_seule_n_ouvre_AUCUNE_issue(atelier):
    # Une passe de lecture qui ouvre des issues n'est pas une passe de lecture.
    depot, runner, profil, store, journal, wt = atelier
    object.__setattr__(runner, "writes_enabled", False)
    w = FauxWriter()
    avec_issues(profil)
    jr = graphe_de(atelier, agent_qui("x = 2\n"), writer=w)[0]

    r = await jr.run(travail_release(), repo_path=depot)

    assert r.dry_run and w.issues == []


# ── Budget du jour ──────────────────────────────────────────────────────────


async def test_le_plafond_du_jour_est_APPLIQUE(atelier):
    # `max_jobs_per_day` etait declare dans la configuration et lu par personne.
    # Un reglage decoratif dans une surface de securite se lit comme une
    # garantie.
    depot, runner, profil, store, journal, wt = atelier
    object.__setattr__(profil.budget, "max_jobs_per_day", 1)
    jr = graphe_de(atelier, agent_qui("x = 2  # ok\n"))[0]

    premier = await jr.run(travail(), repo_path=depot)
    second = await jr.run(travail_release(), repo_path=depot)

    assert premier.state is not State.NEEDS_HUMAN, premier.reason
    assert "plafond du jour" in second.reason
    # Et le bail n'a pas ete pris pour rien : une PR qui clignote en
    # AGENT_WORKING pour un job jamais lance est un faux signal.
    assert store.lease("essai", "backend", 727) is None


async def test_le_mode_lecture_seule_ne_consomme_pas_le_budget(atelier):
    depot, runner, profil, store, journal, wt = atelier
    object.__setattr__(runner, "writes_enabled", False)
    jr = graphe_de(atelier, agent_qui("x = 2\n"))[0]
    await jr.run(travail(), repo_path=depot)
    assert store.jobs_today("essai") == 0


# ── Journal ─────────────────────────────────────────────────────────────────


async def test_le_journal_dit_POURQUOI_le_job_tourne(atelier):
    depot, runner, profil, store, journal, wt = atelier
    jr = graphe_de(atelier, agent_qui("x = 2\n"))[0]
    await jr.run(travail(), repo_path=depot)

    debut = [e for e in journal.recent(50) if e.event == "job.started"]
    assert debut, "un job qui demarre doit se journaliser"
    e = debut[0]
    # La raison vient de `decide`, pas d'une fixture : elle NOMME le fichier
    # et la ligne. C'est ce qu'un humain lit dans le journal pour savoir de
    # quoi le job s'occupe — une raison generique n'aurait rien appris.
    assert e.why == "1 fil(s) ouvert(s) sur app/service.py:3."
    assert e.trigger["threads"] == [1]
    # Le volume de donnee externe est mesure : une remarque de 40 000
    # caracteres n'est pas une remarque, c'est un signal.
    assert "untrusted_chars" in e.trigger


async def test_chaque_verification_est_journalisee(atelier):
    depot, runner, profil, store, journal, wt = atelier
    jr = graphe_de(atelier, agent_qui("x = 2\n"))[0]
    await jr.run(travail(), repo_path=depot)

    # Sans ca, le journal montre un silence de plusieurs minutes suivi d'un
    # verdict, et on ne sait pas si le demon travaille ou s'il est bloque.
    assert [e for e in journal.recent(50) if e.event == "job.check"]


# ── Aucun fil n'est laisse muet ─────────────────────────────────────────────
# Une remarque ignoree ressemble exactement a une remarque perdue, et c'est la
# personne qui l'a ecrite qui paie la difference : elle relance, ou elle
# abandonne. Deux chemins laissaient ce silence.


def _deux_fils():
    return (
        Thread("PRRT_A", 11, "chatgpt-codex-connector", False,
               "**![P1 Badge](x)** le premier", "app/service.py", 3),
        Thread("PRRT_B", 12, "chatgpt-codex-connector", False,
               "**![P1 Badge](x)** le second", "app/service.py", 9),
    )


async def test_un_fil_OUBLIE_par_le_verdict_recoit_quand_meme_une_reponse(atelier):
    # L'agent rend un verdict qui ne parle que du premier fil. Le second
    # restait ouvert, sans un mot — indiscernable d'une remarque perdue.
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    agent = agent_qui("x = 2\n", structure={
        "summary": "corrige le premier",
        "threads": [{"thread_id": "PRRT_A", "outcome": "corrige", "reply": "fait"}],
        "blocked": "",
    })
    jr = graphe_de(atelier, agent, writer=w)[0]

    r = await jr.run(travail(_deux_fils()), repo_path=depot, write_token="jeton")

    repondus = {x[0] for x in w.reponses}
    assert repondus == {"PRRT_A", "PRRT_B"}, "le fil oublie doit etre repondu"
    # Mais il n'est PAS resolu : on ne solde pas ce qu'on n'a pas traite.
    assert w.resolus == ["PRRT_A"]
    assert r.asked >= 1


async def test_le_fil_oublie_DIT_qu_il_attend_un_arbitrage(atelier):
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    agent = agent_qui("x = 2\n", structure={
        "summary": "corrige le premier",
        "threads": [{"thread_id": "PRRT_A", "outcome": "corrige", "reply": "fait"}],
        "blocked": "",
    })
    jr = graphe_de(atelier, agent, writer=w)[0]
    await jr.run(travail(_deux_fils()), repo_path=depot, write_token="jeton")

    muet = next(x for x in w.reponses if x[0] == "PRRT_B")
    # `verdict.parse` le classe en ARBITRAGE : le silence n'est pas un accord.
    assert "arbitrage" in muet[1].lower()
    assert "reste ouvert" in muet[1]
    assert muet[2] is True, "le fil doit etre marque en attente d'un humain"


async def test_un_job_qui_ECHOUE_repond_dans_les_fils(atelier):
    # Un arret ne publiait qu'un commentaire general : la personne qui a ecrit
    # la remarque ne voyait rien dans SON fil et devait deviner qu'un
    # commentaire ailleurs la concernait.
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui(echoue=True), writer=w)[0]

    r = await jr.run(travail(_deux_fils()), repo_path=depot, write_token="jeton")

    assert r.state is State.NEEDS_HUMAN
    assert {x[0] for x in w.reponses} == {"PRRT_A", "PRRT_B"}
    assert w.resolus == [], "un echec ne solde rien"
    # Le commentaire general reste : il porte le motif et la relance.
    assert len(w.commentaires) == 1


async def test_un_echec_ne_REPETE_pas_ses_reponses_a_chaque_cycle(atelier):
    # Sans garde, trois cycles rates feraient trois fois le meme message a la
    # meme personne, dans le meme fil.
    depot, runner, profil, store, journal, wt = atelier
    w = FauxWriter()
    jr = graphe_de(atelier, agent_qui(echoue=True), writer=w)[0]

    await jr.run(travail(_deux_fils()), repo_path=depot, write_token="jeton")
    premier = len(w.reponses)
    await jr.run(travail(_deux_fils()), repo_path=depot, write_token="jeton")

    assert len(w.reponses) == premier, "meme commit, meme echec : on ne repete pas"


# ── Le socle : celui de la PR, pas celui du profil ──────────────────────


def _deps_de(profil):
    """`_derivation` ne lit que le profil : lui donner le reste serait du decor."""
    return SimpleNamespace(profile=profil)


def test_le_socle_d_une_PR_ordinaire_est_SA_base(atelier):
    # `frontend#406`, le 30/08/2026 : un hotfix vise `main`. Rendre la branche
    # d'integration du profil faisait chercher le socle sur `origin/dev` —
    # inexistant dans le clone du conteneur, et trompeur la ou il existe.
    profil = atelier[2]
    snap = PullSnapshot(number=406, repo="frontend", head_sha="abc",
                        head_ref="hotfix/405-carto", base_ref="main")
    assert _derivation(snap, _deps_de(profil)) == (False, "main")


def test_le_socle_retombe_sur_le_profil_quand_la_forge_se_tait(atelier):
    # Une base vide vaudrait `origin/` — un refname invalide. Le socle habituel
    # est un moins mauvais defaut que l'echec.
    profil = atelier[2]
    snap = PullSnapshot(number=714, repo="backend", head_sha="abc",
                        head_ref="fix/714-truc", base_ref="")
    assert _derivation(snap, _deps_de(profil)) == (
        False, profil.forge.integration_branch)


def test_une_tete_PARTAGEE_derive_encore_vers_elle_meme(atelier):
    # Le cas release ne change pas : on derive, et le socle est la tete
    # partagee elle-meme — pas la base de la PR de release, qui est `main`.
    profil = atelier[2]
    snap = PullSnapshot(number=727, repo="backend", head_sha="abc",
                        head_ref="dev", base_ref="main")
    assert _derivation(snap, _deps_de(profil)) == (True, "dev")


# ── Le forcage doit atteindre le noeud `decider` ────────────────────────────


async def test_une_reprise_TRAVERSE_le_graphe_qui_redecide(atelier):
    """Observe sur backend#762 le 31/08.

        16:55:14  sweep.decision  1 fil ouvert           (NEEDS_FIX)
        16:55:15  graph.node      le job demarre
        16:55:16  decide          1 question en attente  (NEEDS_HUMAN)

    Le balayage decidait « il y a du travail » avec le forcage ; le graphe
    RE-DECIDAIT sans lui, retrouvait la question en attente, et le cycle mourait
    juste apres `decider`. Le clic ne produisait qu'un job mort-ne — et la
    console l'affichait « interrompu », sans dire pourquoi.

    Que le graphe re-decide est VOULU : entre le balayage et le job, la forge a
    pu bouger. Mais il doit re-decider sur les MEMES entrees.
    """
    from reviewer.rules.machine import AGENT_MARK, ASK_MARK, Check, Comment

    agent = agent_qui("x = 2\n")
    jr, depot, _ = runner_de(atelier, agent)

    codex = list(atelier[2].reviewers.trust)[0]
    fil = Thread(
        id="PRRT_9", comment_id=1, author=codex, resolved=False,
        body="**![P1 Badge](x)** Ne court-circuitez pas la creation",
        path="app/service.py", line=88,
        comments=(
            Comment(1, codex, "**![P1 Badge](x)** Ne court-circuitez pas la creation"),
            Comment(9, codex, AGENT_MARK + ASK_MARK + " Correctif ecrit, non valide."),
        ),
    )
    snap = PullSnapshot(number=714, repo="backend", head_sha="abc",
                        head_ref="fix/714-truc",
                        checks=(Check("Test backend", "completed", "success"),),
                        threads=(fil,))

    # La decision du BALAYAGE, forcee — celle que le clic produit reellement.
    d = decide(snap, trusted_reviewers=frozenset(atelier[2].reviewers.trust),
               max_review_cycles=atelier[2].max_review_cycles, forced=True)
    assert Action.RUN_AGENT in d.actions, "la fixture ne decrit plus une reprise"

    r = await jr.run(Outcome("backend", 714, d, snap, forced=True),
                     repo_path=depot)

    assert agent.vu, (
        "le graphe a re-decide sans le forcage : le cycle est mort apres "
        "`decider`, exactement comme sur backend#762")
    assert r.state is not State.NEEDS_HUMAN


async def test_sans_reprise_la_question_en_attente_ARRETE_toujours(atelier):
    # Le pendant : sans forcage, une question sans reponse doit continuer
    # d'arreter le cycle. Sinon le correctif aurait supprime le verrou pour
    # tout le monde.
    from reviewer.rules.machine import AGENT_MARK, ASK_MARK, Check, Comment

    agent = agent_qui("x = 2\n")
    jr, depot, _ = runner_de(atelier, agent)
    codex = list(atelier[2].reviewers.trust)[0]
    fil = Thread(
        id="PRRT_9", comment_id=1, author=codex, resolved=False,
        body="**![P1 Badge](x)** Ne court-circuitez pas la creation",
        path="app/service.py", line=88,
        comments=(
            Comment(1, codex, "**![P1 Badge](x)** Ne court-circuitez pas la creation"),
            Comment(9, codex, AGENT_MARK + ASK_MARK + " Correctif ecrit, non valide."),
        ),
    )
    snap = PullSnapshot(number=714, repo="backend", head_sha="abc",
                        head_ref="fix/714-truc",
                        checks=(Check("Test backend", "completed", "success"),),
                        threads=(fil,))
    d = decide(snap, trusted_reviewers=frozenset(atelier[2].reviewers.trust),
               max_review_cycles=atelier[2].max_review_cycles)
    assert d.state is State.NEEDS_HUMAN

    await jr.run(Outcome("backend", 714, d, snap), repo_path=depot)

    assert agent.vu == {}, "sans reprise, l'agent ne doit PAS tourner"
