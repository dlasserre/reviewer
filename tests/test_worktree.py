"""Les worktrees de l'agent restent-ils dans leur perimetre ?

Ces tests montent de VRAIS depots git. C'est le comportement de git qui est en
jeu — enregistrement des worktrees, refus de monter deux fois la meme branche,
chemins absolus stockes — et un double le remplacerait par ce qu'on croit qu'il
fait. Le renommage du workspace du 25/08/2026 a justement montre l'ecart : quatre
worktrees etaient devenus `prunable` sans qu'aucune commande n'echoue.
"""

from __future__ import annotations

from pathlib import Path as _Path

from reviewer.repo.worktree import segments_caches


# ── Un worktree ne se place pas sous un repertoire cache ────────────────────


def test_un_repertoire_cache_sur_le_chemin_est_signale():
    # Mesure du 27/08/2026 : `npm run test:ci` rendait « No tests found » dans
    # un worktree monte sous `.agent-runner/`, pendant que les 131 memes tests
    # passaient dans le depot principal. jest echappe le point du repertoire
    # cache en expansant `<rootDir>`, micromatch lit `\.` comme un point sans
    # separateur, et le motif ne correspond plus a rien.
    #
    # Le runner refusait alors de commiter un correctif juste, cycle apres
    # cycle, pour une raison qui n'avait rien a voir avec le code.
    caches = segments_caches(_Path("C:/x/WebstormProjects/.agent-runner/worktrees"))
    assert caches == (".agent-runner",)


def test_un_chemin_sans_repertoire_cache_ne_signale_rien():
    assert segments_caches(_Path("C:/x/WebstormProjects/agent-runner/worktrees")) == ()


def test_plusieurs_repertoires_caches_sont_tous_rendus():
    caches = segments_caches(_Path("C:/x/.outils/.agent/worktrees"))
    assert set(caches) == {".outils", ".agent"}


def test_le_dernier_segment_compte_aussi():
    # `worktrees_root: .../.worktrees` a exactement le meme effet.
    assert segments_caches(_Path("C:/x/travail/.worktrees")) == (".worktrees",)

import subprocess
import sys
from pathlib import Path

import pytest

from reviewer.repo.worktree import Worktree, WorktreeError, WorktreeManager, _est_lien


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, f"git {' '.join(args)} :\n{r.stderr}"
    return r.stdout


@pytest.fixture
def depot(tmp_path):
    """Un `origin` nu + un clone qui joue le depot principal, avec `dev`."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=dev", ".")

    principal = tmp_path / "atelier" / "backend"
    principal.parent.mkdir(parents=True)
    git(tmp_path / "atelier", "clone", str(origin), "backend")
    git(principal, "config", "user.email", "test@exemple.test")
    git(principal, "config", "user.name", "Test")

    (principal / "app").mkdir()
    (principal / "app" / "service.py").write_text("x = 1\n", encoding="utf-8")
    # Dossiers lourds ignores par git : ce sont eux qu'on montera en jonction.
    (principal / "node_modules").mkdir()
    (principal / "node_modules" / "marqueur.txt").write_text("ne pas perdre", encoding="utf-8")
    (principal / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(principal, "add", "-A")
    git(principal, "commit", "-m", "initial")
    git(principal, "push", "-u", "origin", "dev")
    return principal


@pytest.fixture
def mgr(tmp_path):
    return WorktreeManager(root=tmp_path / ".agent" / "worktrees", profile="essai")


# ── Perimetre ───────────────────────────────────────────────────────────────


def test_le_chemin_est_deterministe(mgr):
    # La session Claude est liee au `cwd` : deplacer le worktree d'une PR entre
    # deux cycles perdrait le contexte qu'on cherche justement a reprendre.
    assert mgr.path_for("backend", 714) == mgr.path_for("backend", 714)
    assert mgr.path_for("backend", 714) != mgr.path_for("backend", 715)
    assert mgr.path_for("backend", 714).name == "essai-backend-pr-714"


def test_un_depot_sous_la_racine_de_l_agent_est_refuse(tmp_path, depot):
    # Deriver un worktree d'un worktree : etat incoherent garanti.
    m = WorktreeManager(root=depot.parent, profile="essai")
    with pytest.raises(WorktreeError, match="SOUS la racine"):
        m.create(depot=depot, repo="backend", pr=1, branch="fix/1-x")


@pytest.mark.parametrize("branche", ["dev", "main", "master"])
def test_une_branche_partagee_est_refusee(mgr, depot, branche):
    with pytest.raises(WorktreeError, match="branche partagee"):
        mgr.create(depot=depot, repo="backend", pr=1, branch=branche)


# ── Creation ────────────────────────────────────────────────────────────────


def test_un_worktree_est_cree_et_ENREGISTRE(mgr, depot):
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    assert wt.path.is_dir()
    assert (wt.path / "app" / "service.py").is_file()
    # L'enregistrement est verifie explicitement : c'est ce qui manquait le
    # 25/08, ou quatre worktrees etaient `prunable` sans qu'on le sache.
    assert wt.path.resolve() in mgr.existing_worktrees(depot)
    assert git(depot, "worktree", "list").count("prunable") == 0


def test_la_branche_est_creee_depuis_la_base(mgr, depot):
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x", base="dev")
    assert git(wt.path, "branch", "--show-current").strip() == "fix/714-x"


def test_une_branche_existante_est_reutilisee(mgr, depot):
    git(depot, "branch", "fix/714-deja")
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-deja")
    assert git(wt.path, "branch", "--show-current").strip() == "fix/714-deja"


def test_creer_deux_fois_est_idempotent(mgr, depot):
    # Un cycle de correction doit reprendre le meme arbre — sinon la session
    # Claude attachee au `cwd` est perdue a chaque passage.
    a = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    b = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    assert a.path == b.path
    assert len(mgr.existing_worktrees(depot)) == 2      # principal + le notre


def test_le_meme_chemin_sur_une_AUTRE_branche_est_refuse(mgr, depot):
    mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    with pytest.raises(WorktreeError, match="se disputent le meme arbre"):
        mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-autre")


def test_une_branche_deja_montee_ailleurs_est_refusee(mgr, depot, tmp_path):
    # Cas reel : l'humain a deja un worktree sur cette branche. Le bail ne
    # protege que de deux JOBS, pas d'un job et d'un humain.
    ailleurs = tmp_path / "atelier" / "backend-wt-714"
    git(depot, "worktree", "add", "-b", "fix/714-x", str(ailleurs), "dev")
    with pytest.raises(WorktreeError, match="deja montee dans"):
        mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")


def test_un_reste_de_job_interrompu_est_signale(mgr, depot):
    cible = mgr.path_for("backend", 714)
    cible.mkdir(parents=True)
    (cible / "traine.txt").write_text("reste", encoding="utf-8")
    with pytest.raises(WorktreeError, match="job interrompu"):
        mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")


# ── Jonctions ───────────────────────────────────────────────────────────────


def test_les_dossiers_lourds_sont_montes(mgr, depot):
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    assert "node_modules" in wt.linked
    assert wt.prepared
    # Le contenu du depot principal est visible depuis le worktree : c'est tout
    # l'interet — sinon chaque job repart d'un `npm install` complet.
    assert (wt.path / "node_modules" / "marqueur.txt").read_text(encoding="utf-8") \
        == "ne pas perdre"


def test_un_dossier_absent_du_depot_n_est_pas_signale_manquant(mgr, depot):
    # `corpus` n'existe pas dans ce depot : il n'y a rien a monter, ce n'est
    # pas un manque.
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    assert "corpus" not in wt.linked
    assert "corpus" not in wt.missing_links


@pytest.mark.skipif(sys.platform != "win32", reason="jonctions Windows")
def test_une_jonction_est_reconnue_comme_un_lien(mgr, depot):
    # `os.path.islink` rend False sur une jonction : s'y fier ferait prendre la
    # jonction pour un dossier ordinaire, et l'effacer effacerait la CIBLE.
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    lien = wt.path / "node_modules"
    assert _est_lien(lien), "la jonction doit etre reconnue"
    assert not _est_lien(wt.path / "app"), "un vrai dossier n'est pas un lien"


# ── Retrait ─────────────────────────────────────────────────────────────────


def test_retirer_ne_DETRUIT_PAS_la_cible_de_la_jonction(mgr, depot):
    # LE test qui compte : le worktree part, le contenu du depot principal
    # reste. Un demontage qui traverserait le lien couterait `node_modules`,
    # `corpus` ou `artifacts` — des dossiers qui ne sont pas dans git.
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    temoin = depot / "node_modules" / "marqueur.txt"
    assert temoin.is_file()

    mgr.remove(wt, depot=depot)

    assert not wt.path.exists()
    assert temoin.is_file(), "le contenu du depot principal a ete detruit"
    assert temoin.read_text(encoding="utf-8") == "ne pas perdre"


def test_retirer_refuse_si_du_travail_y_dort(mgr, depot):
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    (wt.path / "app" / "service.py").write_text("x = 2  # en cours\n", encoding="utf-8")
    with pytest.raises(WorktreeError, match="non commite"):
        mgr.remove(wt, depot=depot)
    assert wt.path.is_dir(), "le worktree doit survivre au refus"


def test_retirer_de_force_passe_outre(mgr, depot):
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    (wt.path / "app" / "service.py").write_text("x = 2\n", encoding="utf-8")
    mgr.remove(wt, depot=depot, force=True)
    assert not wt.path.exists()
    assert (depot / "node_modules" / "marqueur.txt").is_file()


def test_apres_retrait_git_ne_garde_pas_de_fantome(mgr, depot):
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    mgr.remove(wt, depot=depot)
    liste = git(depot, "worktree", "list")
    assert "pr-714" not in liste
    assert "prunable" not in liste


def test_on_peut_recreer_apres_retrait(mgr, depot):
    wt = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    mgr.remove(wt, depot=depot)
    encore = mgr.create(depot=depot, repo="backend", pr=714, branch="fix/714-x")
    assert encore.path.is_dir()


# ── Diagnostic ──────────────────────────────────────────────────────────────


def test_un_montage_manquant_est_rendu_et_non_avale(tmp_path, depot):
    # Un montage manque produit un echec de test qui RESSEMBLE a un bug du
    # code. Il doit donc etre visible avant qu'on accuse l'agent.
    m = WorktreeManager(root=tmp_path / ".agent" / "wt", profile="essai")
    wt = m.create(depot=depot, repo="backend", pr=1, branch="fix/1-x")
    faux = Worktree(path=wt.path, branch="fix/1-x", repo="backend", base="dev",
                    missing_links=("node_modules",))
    assert not faux.prepared


# ── Les fichiers d'environnement, absents d'un worktree neuf ────────────────


def test_les_fichiers_d_env_sont_COPIES_dans_le_worktree(depot, tmp_path):
    # Mesure du 29/08/2026 : un worktree ne contient que ce que git SUIT. Les
    # `.env*` sont ignores, donc absents — et `conftest.py` les exige. pytest
    # s'est arrete sur 336 erreurs DE COLLECTION, code 2, avant d'avoir lance
    # un seul test. L'agent avait pourtant ecrit et pousse un correctif juste.
    (depot / ".env").write_text("CLE=valeur\n", encoding="utf-8")
    m = WorktreeManager(root=tmp_path / "wt", profile="essai")

    wt = m.create(depot=depot, repo="backend", pr=42,
                  branch="fix/42-truc", base="dev")

    assert ".env" in wt.linked
    assert (wt.path / ".env").read_text(encoding="utf-8") == "CLE=valeur\n"


def test_le_fichier_d_env_est_une_COPIE_pas_une_jonction(depot, tmp_path):
    # Une jonction rendrait le vrai `.env` du depot accessible en ecriture
    # depuis le worktree. Le garde-fou `PreToolUse` l'interdit deja, mais une
    # capacite absente vaut mieux qu'une capacite interdite.
    (depot / ".env").write_text("ORIGINAL=1\n", encoding="utf-8")
    m = WorktreeManager(root=tmp_path / "wt", profile="essai")
    wt = m.create(depot=depot, repo="backend", pr=43,
                  branch="fix/43-truc", base="dev")

    (wt.path / ".env").write_text("MODIFIE=1\n", encoding="utf-8")

    assert (depot / ".env").read_text(encoding="utf-8") == "ORIGINAL=1\n"


def test_un_env_ABSENT_du_depot_ne_fait_rien_echouer(depot, tmp_path):
    # Un depot sans `.env` — le cas de `frontend` — ne doit pas voir son
    # worktree refuse pour un fichier qu'il n'a jamais eu.
    m = WorktreeManager(root=tmp_path / "wt", profile="essai")
    wt = m.create(depot=depot, repo="backend", pr=44,
                  branch="fix/44-truc", base="dev")
    assert wt.prepared
    assert ".env" not in wt.linked


# ── La tete d'une PR vient d'origin, pas du socle ────────────────────────


def test_une_branche_qui_n_existe_que_sur_origin_est_RECUPEREE(mgr, depot):
    """Le cas ORDINAIRE : la PR a ete ouverte depuis une AUTRE machine.

    Tous les autres tests de ce fichier creent la branche en local avant
    d'appeler `create` — c'est exactement pour ca que le defaut a pu vivre :
    ils prenaient tous le chemin « branche deja la ». Ici elle n'existe que sur
    origin, comme n'importe quelle PR que le demon decouvre.
    """
    git(depot, "checkout", "-q", "-b", "hotfix/405-venue-d-ailleurs")
    (depot / "app" / "correctif.py").write_text("cle = 1\n", encoding="utf-8")
    git(depot, "add", "-A")
    git(depot, "commit", "-m", "le correctif porte par la PR")
    git(depot, "push", "-q", "-u", "origin", "hotfix/405-venue-d-ailleurs")
    git(depot, "checkout", "-q", "dev")
    git(depot, "branch", "-D", "hotfix/405-venue-d-ailleurs")

    wt = mgr.create(depot=depot, repo="frontend", pr=406,
                    branch="hotfix/405-venue-d-ailleurs", base="dev")

    assert git(wt.path, "branch", "--show-current").strip() == "hotfix/405-venue-d-ailleurs"
    # LE point du test, et la raison d'etre du fichier temoin : la version
    # fautive creait bien une branche du bon NOM — assise sur le socle. Sans
    # verifier le CONTENU, elle serait passee au vert en relisant `dev`.
    assert (wt.path / "app" / "correctif.py").exists(), (
        "le worktree porte le nom de la PR mais le code du socle"
    )


def test_une_branche_INCONNUE_part_toujours_du_socle(mgr, depot):
    # Le cas derive (PR de release) ne bouge pas : la branche n'existe ni ici
    # ni sur origin, et c'est la SEULE situation ou le socle a un sens.
    wt = mgr.create(depot=depot, repo="backend", pr=727,
                    branch="fix/pr727-revue", base="dev")
    assert git(wt.path, "branch", "--show-current").strip() == "fix/pr727-revue"
    assert (git(wt.path, "rev-parse", "HEAD").strip()
            == git(depot, "rev-parse", "origin/dev").strip())
