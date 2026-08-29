"""La ligne de commande DEMARRE-t-elle ?

Ce module existe a cause d'un trou reel, trouve le 27/08/2026 : aucun test
n'importait `__main__`. Une erreur de syntaxe y est donc restee invisible a une
suite entierement verte — 464 tests passaient sur un binaire qui ne se lancait
pas.

Le garde-fou d'hygiene ne pouvait pas la voir non plus : il LIT les sources et
cherche des caracteres de controle, il ne les compile pas. Un saut de ligne reel
au milieu d'un litteral de chaine est un caractere parfaitement legitime — a
l'endroit ou il se trouvait, c'etait une erreur de syntaxe.

Ces tests ne verifient donc pas une logique fine : ils verifient que le point
d'entree existe, se compile, et rend ce qu'on attend. C'est peu, et c'est
exactement ce qui manquait.
"""

from __future__ import annotations

import asyncio
import compileall
import textwrap
from pathlib import Path

import pytest

from agent_runner_lg.graph.sweep import ordonnancer
from agent_runner_lg.rules.machine import Action, Decision, PullSnapshot, State
from agent_runner_lg.graph.sweep import Outcome

from agent_runner_lg.__main__ import _diagnostic, main
from agent_runner_lg.config import load_profile, load_runner

RACINE = Path(__file__).resolve().parent.parent


def test_toutes_les_sources_se_compilent():
    # Le complement du garde-fou d'hygiene : lui cherche l'invisible, celui-ci
    # cherche l'invalide.
    assert compileall.compile_dir(str(RACINE / "src"), quiet=2, force=True)


@pytest.fixture
def config(tmp_path, monkeypatch):
    t = str(tmp_path).replace("\\", "/")
    (tmp_path / "profils").mkdir()
    (tmp_path / "runner.yaml").write_text(textwrap.dedent(f"""
        worktrees_root: {t}/wt
        state_db: {t}/state.db
        logs_dir: {t}/logs
        profiles_dir: {t}/profils
        writes_enabled: true
        claude:
          oauth_token: env:UN_JETON_CLAUDE
    """), encoding="utf-8")
    (tmp_path / "profils" / "p.yaml").write_text(textwrap.dedent(f"""
        project: essai
        workspace: {t}/ws
        forge:
          org: UneOrg
          token_read: env:UN_JETON_LECTURE
          token_write: env:UN_JETON_ECRITURE
        human:
          notify: "@quelquun"
        repos:
          backend:
            access: write
            path: {t}/ws/backend
            checks: [ 'echo ok' ]
    """), encoding="utf-8")
    (tmp_path / "ws" / "backend").mkdir(parents=True)
    for nom in ("UN_JETON_CLAUDE", "UN_JETON_LECTURE", "UN_JETON_ECRITURE"):
        monkeypatch.setenv(nom, "x" * 20)
    return tmp_path


def test_la_commande_check_se_lance_et_reussit(config, capsys):
    assert main(["-c", str(config / "runner.yaml"), "check"]) == 0
    sortie = capsys.readouterr().out
    assert "runner.yaml : OK" in sortie
    assert "profil essai" in sortie


def test_check_dit_que_RIEN_ne_manque_quand_tout_est_la(config, capsys):
    main(["-c", str(config / "runner.yaml"), "check"])
    assert "Rien ne manque" in capsys.readouterr().out


def test_une_configuration_illisible_ne_plante_pas_mais_rend_2(tmp_path, capsys):
    (tmp_path / "runner.yaml").write_text("worktrees_root: [", encoding="utf-8")
    assert main(["-c", str(tmp_path / "runner.yaml"), "check"]) == 2
    assert "Configuration invalide" in capsys.readouterr().err


# ── Le diagnostic : valide n'est pas operant ────────────────────────────────


def test_un_profil_SANS_jeton_d_ecriture_est_signale(config, monkeypatch):
    # Le cas exact du 27/08/2026 : `check` disait OK sur une configuration ou
    # l'agent ne pouvait ni pousser, ni ouvrir de PR, ni repondre a un fil.
    texte = (config / "profils" / "p.yaml").read_text(encoding="utf-8")
    (config / "profils" / "p.yaml").write_text(
        texte.replace("  token_write: env:UN_JETON_ECRITURE\n", ""),
        encoding="utf-8")
    profil = load_profile(config / "profils" / "p.yaml")
    runner = load_runner(config / "runner.yaml")
    object.__setattr__(runner, "writes_enabled", True)

    manques = _diagnostic(profil, runner)
    assert any("token_write" in m for m in manques)
    assert any("invisible" in m for m in manques)


def test_une_variable_ABSENTE_est_signalee_comme_telle(config, monkeypatch):
    monkeypatch.delenv("UN_JETON_ECRITURE")
    profil = load_profile(config / "profils" / "p.yaml")
    runner = load_runner(config / "runner.yaml")
    object.__setattr__(runner, "writes_enabled", True)
    assert any("UN_JETON_ECRITURE" in m for m in _diagnostic(profil, runner))


def test_le_jeton_du_SDK_manquant_est_signale_AVANT_le_lancement(config, monkeypatch):
    # Sans ce controle, l'absence se manifeste comme « l'agent n'a pas conclu »
    # — un message qui envoie chercher la cause dans le prompt ou le modele.
    monkeypatch.delenv("UN_JETON_CLAUDE")
    profil = load_profile(config / "profils" / "p.yaml")
    runner = load_runner(config / "runner.yaml")
    manques = _diagnostic(profil, runner)
    assert any("UN_JETON_CLAUDE" in m and "aucun agent" in m for m in manques)


def test_le_mode_lecture_seule_est_signale(config):
    profil = load_profile(config / "profils" / "p.yaml")
    runner = load_runner(config / "runner.yaml")
    object.__setattr__(runner, "writes_enabled", False)
    assert any("writes_enabled" in m for m in _diagnostic(profil, runner))


# ── Le moteur : modele et effort ───────────────────────────────────────────


def test_le_moteur_par_defaut_est_DIT_meme_quand_il_est_implicite(config):
    # Les deux premiers jobs reels ont tourne sur `claude-sonnet-5` sans que
    # rien ne l'annonce : il a fallu relire un transcrit de session pour le
    # savoir. Un reglage qui gouverne le cout et la qualite se lit dans la
    # sortie, y compris quand personne ne l'a choisi.
    from agent_runner_lg.__main__ import _dire_le_moteur

    profil = load_profile(config / "profils" / "p.yaml")
    ligne = _dire_le_moteur(profil)
    assert "modele=defaut du CLI" in ligne
    assert "effort=defaut du CLI" in ligne
    assert "max_turns=" in ligne


def test_la_surcharge_de_ligne_de_commande_gagne_sur_le_profil(config):
    from agent_runner_lg.__main__ import _surcharger

    profils = {"essai": load_profile(config / "profils" / "p.yaml")}
    sortie = _surcharger(profils, model="claude-opus-5", effort="high")

    assert sortie["essai"].model == "claude-opus-5"
    assert sortie["essai"].effort == "high"
    # Et le reste du profil est intact : la surcharge ne reconstruit pas un
    # profil au rabais.
    assert sortie["essai"].repos["backend"].checks == ["echo ok"]
    assert sortie["essai"].human.notify == "@quelquun"


def test_sans_surcharge_les_profils_sont_rendus_TELS_QUELS(config):
    # Reconstruire pour rien, c'est une occasion de perdre quelque chose.
    from agent_runner_lg.__main__ import _surcharger

    profils = {"essai": load_profile(config / "profils" / "p.yaml")}
    assert _surcharger(profils, model=None, effort=None) is profils


def test_un_effort_INCONNU_est_refuse_avec_les_valeurs_acceptees(config):
    from agent_runner_lg.__main__ import _surcharger
    from agent_runner_lg.config import ConfigError

    profils = {"essai": load_profile(config / "profils" / "p.yaml")}
    with pytest.raises(ConfigError, match="low, medium, high, xhigh, max"):
        _surcharger(profils, model=None, effort="turbo")


def test_un_modele_VIDE_n_est_pas_un_choix(tmp_path):
    # La chaine vide passerait pour une decision alors qu'elle retombe sur le
    # defaut du CLI sans le dire.
    from agent_runner_lg.config import ConfigError, load_profile as charger

    chemin = tmp_path / "p.yaml"
    chemin.write_text(textwrap.dedent("""
        project: essai
        workspace: /tmp/ws
        forge:
          org: UneOrg
        model: "  "
    """), encoding="utf-8")
    with pytest.raises(ConfigError, match="n'est pas un choix"):
        charger(chemin)


def test_le_moteur_choisi_apparait_dans_la_ligne_de_bilan(config):
    from agent_runner_lg.__main__ import _dire_le_moteur, _surcharger

    profils = _surcharger({"essai": load_profile(config / "profils" / "p.yaml")},
                          model="claude-opus-5", effort="max")
    ligne = _dire_le_moteur(profils["essai"])
    assert "modele=claude-opus-5" in ligne and "effort=max" in ligne


# ── Contradictions refusees au CHARGEMENT ──────────────────────────────────


def _profil(tmp_path, corps: str):
    chemin = tmp_path / "contradictoire.yaml"
    chemin.write_text(textwrap.dedent(corps), encoding="utf-8")
    return chemin


def test_une_branche_nommee_d_apres_une_issue_QUI_N_EXISTERA_PAS_est_refusee(tmp_path):
    # La contradiction se paierait tard, sinon : le nom de branche ne se calcule
    # qu'au moment de monter le worktree, donc en plein job, apres avoir pris un
    # bail et consomme un cycle.
    from agent_runner_lg.config import ConfigError, load_profile

    chemin = _profil(tmp_path, """
        project: essai
        workspace: /tmp/ws
        forge:
          org: UneOrg
        derived_branch: "fix/{issue}-revue"
        issues:
          enabled: false
    """)
    with pytest.raises(ConfigError, match="issues.enabled"):
        load_profile(chemin)


def test_une_priorite_URGENT_automatique_est_refusee(tmp_path):
    # Cette valeur ouvre la voie hotfix. Une voie d'urgence qu'un automate peut
    # declarer n'est plus une voie d'urgence.
    from agent_runner_lg.config import ConfigError, load_profile

    chemin = _profil(tmp_path, """
        project: essai
        workspace: /tmp/ws
        forge:
          org: UneOrg
        issues:
          enabled: true
          priority_by_severity:
            P1: Urgent
    """)
    with pytest.raises(ConfigError, match="urgence"):
        load_profile(chemin)


def test_une_branche_derivee_sans_aucun_numero_est_refusee(tmp_path):
    # Deux PR de release produiraient la meme branche, et git refuse deux
    # worktrees sur une meme reference.
    from agent_runner_lg.config import ConfigError, load_profile

    chemin = _profil(tmp_path, """
        project: essai
        workspace: /tmp/ws
        forge:
          org: UneOrg
        derived_branch: "fix/revue"
    """)
    with pytest.raises(ConfigError, match=r"\{pr\}"):
        load_profile(chemin)


def test_l_absence_de_mention_est_signalee(config):
    texte = (config / "profils" / "p.yaml").read_text(encoding="utf-8")
    (config / "profils" / "p.yaml").write_text(
        texte.replace('human:\n  notify: "@quelquun"\n', ""), encoding="utf-8")
    profil = load_profile(config / "profils" / "p.yaml")
    runner = load_runner(config / "runner.yaml")
    object.__setattr__(runner, "writes_enabled", True)
    assert any("notify" in m for m in _diagnostic(profil, runner))


# ── L'ordonnancement des jobs menes de front ────────────────────────────────


def _o(repo: str, numero: int, *, tete: str = "fix/x", actionable: bool = True):
    """Sortie de reconciliation minimale : l'ordonnanceur ne lit que ca."""
    actions = (Action.RUN_AGENT,) if actionable else (Action.ASK_HUMAN,)
    return Outcome(repo, numero,
                   Decision(State.NEEDS_FIX, "peu importe", actions),
                   PullSnapshot(number=numero, repo=repo, head_sha="abc",
                                head_ref=tete))


PARTAGEES = frozenset({"dev", "main"})


def test_deux_depots_differents_passent_de_front():
    # Le cas courant ici : une PR par depot. Deux depots n'ont pas de `.git`
    # commun, donc rien a se disputer.
    vagues, reportes = ordonnancer(
        [_o("backend", 1), _o("mobile", 2)],
        max_parallel=3, shared_refs=PARTAGEES, restant=5)
    assert [len(v) for v in vagues] == [2]
    assert reportes == []


def test_deux_PR_DU_MEME_DEPOT_ne_passent_jamais_ensemble():
    # Elles partagent le `.git` du depot : `fetch`, `worktree add` et
    # `worktree remove` y ecrivent tous. Les mener de front, c'est courir apres
    # une corruption d'index pour gagner quelques minutes.
    vagues, _ = ordonnancer(
        [_o("backend", 1), _o("backend", 2)],
        max_parallel=3, shared_refs=PARTAGEES, restant=5)
    assert [len(v) for v in vagues] == [1, 1]


def test_une_PR_de_livraison_passe_SEULE():
    # Sa tete est partagee : le job derive une branche, cree une issue et ouvre
    # une PR distincte. Ce chemin touche plus de choses qu'un correctif
    # ordinaire — on le regarde sans rien d'autre autour.
    vagues, _ = ordonnancer(
        [_o("backend", 1), _o("frontend", 2, tete="dev"), _o("mobile", 3)],
        max_parallel=3, shared_refs=PARTAGEES, restant=5)
    assert [len(v) for v in vagues] == [1, 1, 1]
    assert vagues[1][0].number == 2


def test_le_parallelisme_est_borne():
    vagues, _ = ordonnancer(
        [_o("a", 1), _o("b", 2), _o("c", 3), _o("d", 4)],
        max_parallel=2, shared_refs=PARTAGEES, restant=9)
    assert [len(v) for v in vagues] == [2, 2]


def test_max_parallel_a_1_redonne_le_comportement_sequentiel():
    vagues, _ = ordonnancer(
        [_o("a", 1), _o("b", 2)],
        max_parallel=1, shared_refs=PARTAGEES, restant=9)
    assert [len(v) for v in vagues] == [1, 1]


def test_le_plafond_reporte_le_surplus_et_le_DIT():
    vagues, reportes = ordonnancer(
        [_o("a", 1), _o("b", 2), _o("c", 3)],
        max_parallel=3, shared_refs=PARTAGEES, restant=2)
    assert sum(len(v) for v in vagues) == 2
    assert [o.number for o in reportes] == [3]


def test_une_PR_qui_n_a_qu_une_QUESTION_ne_consomme_pas_le_plafond():
    # Elle ne lance aucun agent : la compter ferait taire des arbitrages pour
    # rien, alors qu'ils ne coutent ni quota ni temps machine.
    vagues, reportes = ordonnancer(
        [_o("a", 1, actionable=False), _o("b", 2, actionable=False), _o("c", 3)],
        max_parallel=3, shared_refs=PARTAGEES, restant=1)
    assert reportes == []
    assert sum(len(v) for v in vagues) == 3


def _runner_avec(tmp_path, *, reconcile_every: str):
    """Un `RunnerConfig` minimal, dont seul l'intervalle compte ici."""
    t = str(tmp_path).replace("\\", "/")
    f = tmp_path / "r.yaml"
    f.write_text(textwrap.dedent(f"""
        worktrees_root: {t}/wt
        state_db: {t}/state.db
        logs_dir: {t}/logs
        wake:
          reconcile_every: {reconcile_every}
    """), encoding="utf-8")
    return load_runner(f)


# ── `serve` travaille-t-il vraiment ? ───────────────────────────────────────
# Il ne le faisait pas. Il exposait l'API et rien d'autre, en le disant a
# l'ecran — mais un demon qu'on laisse tourner une nuit est cense travailler.
# Trois revues ont attendu une journee sur `backend#727` pendant que le
# processus repondait a `/health`.


async def test_la_boucle_appelle_le_travail_a_chaque_tour(monkeypatch, tmp_path):
    import agent_runner_lg.__main__ as m

    passages = []

    async def faux_run(runner, profils, only, limit):
        passages.append(limit)
        if len(passages) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(m, "_run", faux_run)
    runner = _runner_avec(tmp_path, reconcile_every="1s")
    with pytest.raises(asyncio.CancelledError):
        await m._boucle_de_travail(runner, {}, 2)
    assert passages == [2, 2, 2]


async def test_le_premier_passage_est_IMMEDIAT(monkeypatch, tmp_path):
    # Attendre l'intervalle avant le premier tour ferait croire, au lancement,
    # que le demon ne voit rien — precisement le symptome qu'on corrige.
    import agent_runner_lg.__main__ as m

    vu = []

    async def faux_run(runner, profils, only, limit):
        vu.append(True)
        raise asyncio.CancelledError

    monkeypatch.setattr(m, "_run", faux_run)
    runner = _runner_avec(tmp_path, reconcile_every="1h")
    with pytest.raises(asyncio.CancelledError):
        await m._boucle_de_travail(runner, {}, 1)
    assert vu == [True]


async def test_une_erreur_n_arrete_PAS_la_boucle(monkeypatch, tmp_path, capsys):
    # Jeton expire, GitHub indisponible, coupure reseau : le passage suivant
    # reessaie. Un demon qui meurt sur la premiere erreur transitoire est pire
    # qu'absent — on le croit vivant.
    import agent_runner_lg.__main__ as m

    tours = []

    async def faux_run(runner, profils, only, limit):
        tours.append(len(tours))
        if len(tours) == 1:
            raise RuntimeError("GitHub injoignable")
        raise asyncio.CancelledError

    monkeypatch.setattr(m, "_run", faux_run)
    runner = _runner_avec(tmp_path, reconcile_every="1s")
    with pytest.raises(asyncio.CancelledError):
        await m._boucle_de_travail(runner, {}, 1)
    assert len(tours) == 2
    # Et l'erreur est DITE : sans ca, un demon qui echoue a chaque passage est
    # indiscernable d'un demon qui n'a rien a faire.
    assert "GitHub injoignable" in capsys.readouterr().err
