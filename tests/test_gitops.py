"""Le runner refuse-t-il de rendre visible ce qui ne devrait pas l'etre ?

Commit et push sont les deux gestes dont on ne revient pas facilement. Les tests
portent donc sur les REFUS, pas sur le chemin nominal : un commit qui marche se
voit tout de suite, un refus qui manque ne se voit qu'apres.

Vrais depots git : c'est le comportement de git qui est en jeu.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reviewer.repo.git import Diff, GitError, commit_all, current_branch, diff_stat, push


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, f"git {' '.join(args)} :\n{r.stderr}"
    return r.stdout


@pytest.fixture
def arbre(tmp_path):
    """Un depot sur une branche de travail, avec un commit initial."""
    d = tmp_path / "depot"
    d.mkdir()
    git(d, "init", "--initial-branch=dev", ".")
    git(d, "config", "user.email", "t@t.test")
    git(d, "config", "user.name", "T")
    (d / "app").mkdir()
    (d / "app" / "service.py").write_text("x = 1\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-m", "initial")
    git(d, "checkout", "-q", "-b", "fix/714-truc")
    return d


# ── Detection du diff ───────────────────────────────────────────────────────


def test_un_arbre_propre_n_a_rien_a_commiter(arbre):
    assert diff_stat(arbre).empty


def test_une_modification_est_vue(arbre):
    (arbre / "app" / "service.py").write_text("x = 2\n", encoding="utf-8")
    d = diff_stat(arbre)
    assert d.files == ("app/service.py",)
    assert (d.insertions, d.deletions) == (1, 1)


def test_un_fichier_NOUVEAU_est_vu(arbre):
    # `git diff --stat` seul ignore les fichiers non suivis : un agent qui cree
    # un test verrait son travail declare vide.
    (arbre / "app" / "nouveau.py").write_text("y = 1\n", encoding="utf-8")
    d = diff_stat(arbre)
    assert "app/nouveau.py" in d.files
    assert not d.empty


def test_le_resume_est_lisible(arbre):
    (arbre / "app" / "service.py").write_text("x = 2\ny = 3\n", encoding="utf-8")
    assert "fichier(s)" in diff_stat(arbre).summary()
    assert diff_stat(tmp := arbre).summary() != "aucune modification"


# ── Chemins proteges ────────────────────────────────────────────────────────


@pytest.mark.parametrize("chemin", [
    ".github/workflows/ci.yml",
    "deploy/nginx.conf",
    ".env",
    "cles/prod.pem",
])
def test_un_diff_qui_touche_un_chemin_protege_est_refuse(arbre, chemin):
    f = arbre / chemin
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("contenu\n", encoding="utf-8")
    with pytest.raises(GitError, match="protege"):
        commit_all(arbre, "fix(ci): quelque chose")


def test_le_message_nomme_les_chemins_refuses(arbre):
    f = arbre / ".github" / "workflows" / "ci.yml"
    f.parent.mkdir(parents=True)
    f.write_text("on: push\n", encoding="utf-8")
    with pytest.raises(GitError) as e:
        commit_all(arbre, "fix(ci): x")
    assert ".github/workflows/ci.yml" in str(e.value)


def test_un_diff_ordinaire_passe(arbre):
    (arbre / "app" / "service.py").write_text("x = 2\n", encoding="utf-8")
    d = commit_all(arbre, "fix(service): corriger la valeur")
    assert d.files == ("app/service.py",)
    assert diff_stat(arbre).empty       # tout a bien ete commite


# ── Branches ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ref", ["dev", "main"])
def test_commiter_sur_une_branche_partagee_est_refuse(arbre, ref):
    git(arbre, "checkout", "-q", "-B", ref)
    (arbre / "app" / "service.py").write_text("x = 3\n", encoding="utf-8")
    with pytest.raises(GitError, match="branche partagee"):
        commit_all(arbre, "fix(service): x")


def test_une_HEAD_detachee_est_refusee(arbre):
    git(arbre, "checkout", "-q", "--detach")
    (arbre / "app" / "service.py").write_text("x = 4\n", encoding="utf-8")
    with pytest.raises(GitError, match="detachee"):
        commit_all(arbre, "fix(service): x")


def test_la_branche_courante_est_lue(arbre):
    assert current_branch(arbre) == "fix/714-truc"


# ── Message de commit ───────────────────────────────────────────────────────


@pytest.mark.parametrize("message", [
    "corrige le truc",                 # pas de type
    "fix le truc",                     # pas de deux-points
    "fix: ab",                         # sujet trop court
    "Fix(service) corriger",           # pas de deux-points
])
def test_un_sujet_non_conventionnel_est_refuse(arbre, message):
    (arbre / "app" / "service.py").write_text("x = 5\n", encoding="utf-8")
    with pytest.raises(GitError, match="conventionnel"):
        commit_all(arbre, message)


@pytest.mark.parametrize("message", [
    "fix(service): corriger la projection",
    "feat: ajouter le bloc fruit",
    "refactor(api)!: renommer la route",
    "docs: preciser le chemin",
])
def test_un_sujet_conventionnel_passe(arbre, message):
    (arbre / "app" / "service.py").write_text(f"x = {len(message)}\n", encoding="utf-8")
    commit_all(arbre, message)


def test_rien_a_commiter_est_une_erreur_explicite(arbre):
    # Un « commit » qui ne commite rien et ne dit rien laisserait croire que le
    # travail est passe.
    with pytest.raises(GitError, match="rien a commiter"):
        commit_all(arbre, "fix(service): x")


# ── Push ────────────────────────────────────────────────────────────────────


def test_le_push_force_est_refuse(arbre):
    with pytest.raises(GitError, match="force"):
        push(arbre, token="peu importe", force=True)


@pytest.mark.parametrize("ref", ["dev", "main", "master"])
def test_pousser_une_branche_partagee_est_refuse(arbre, ref):
    git(arbre, "checkout", "-q", "-B", ref)
    with pytest.raises(GitError, match="branche partagee"):
        push(arbre, token="peu importe")


def test_le_jeton_ne_reste_pas_dans_la_configuration(tmp_path, arbre):
    # Le jeton passe par un en-tete EPHEMERE : il ne doit survivre ni dans
    # `.git/config`, ni dans l'URL du remote.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=dev", ".")
    git(arbre, "remote", "add", "origin", str(origin))

    (arbre / "app" / "service.py").write_text("x = 9\n", encoding="utf-8")
    commit_all(arbre, "fix(service): pousser")
    push(arbre, token="JETON-SECRET-A-NE-PAS-RETROUVER")

    config = (arbre / ".git" / "config").read_text(encoding="utf-8")
    assert "JETON-SECRET" not in config
    assert "JETON-SECRET" not in git(arbre, "remote", "-v")
    # Et le push a bien eu lieu.
    assert "fix/714-truc" in git(origin, "branch", "--list")


# ── Le type Diff ────────────────────────────────────────────────────────────


def test_un_diff_vide_le_dit():
    assert Diff().empty
    assert Diff().summary() == "aucune modification"


def test_les_motifs_proteges_se_reconnaissent_avec_des_antislashs():
    # Sous Windows, git rend des slashs mais un chemin construit localement
    # peut porter des antislashs.
    d = Diff((r".github\workflows\ci.yml",))
    assert d.protected()


# ── Le jeton ne doit jamais atteindre le journal ────────────────────────────


def test_un_entete_d_autorisation_est_MASQUE():
    """Le 31/08/2026, un push rejete a ecrit le PAT complet dans le journal.

    `git -c http.extraheader=AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46<PAT>`
    — `eC1hY2Nlc3MtdG9rZW46` est `x-access-token:` en base64, donc la suite EST
    le jeton. Ecrit sur le disque, rendu dans la console du navigateur.
    """
    from reviewer.repo.git import sans_secret

    brut = ("git -c http.extraheader=AUTHORIZATION: basic "
            "eC1hY2Nlc3MtdG9rZW46Z2l0aHViX3BhdF9TRUNSRVQ= push origin x")
    propre = sans_secret(brut)

    assert "Z2l0aHViX3BhdF9TRUNSRVQ=" not in propre
    assert "eC1hY2Nlc3MtdG9rZW46" not in propre
    # La CLE reste : un message qui ne dit plus de quoi il parle ne sert a rien.
    assert "AUTHORIZATION" in propre
    assert "push origin x" in propre


def test_le_push_ne_met_PAS_le_jeton_dans_argv(monkeypatch, tmp_path):
    """Meme masque, `argv` reste lisible par tout processus de la machine.

    Le jeton passe donc par l'ENVIRONNEMENT (`GIT_CONFIG_*`), que seul le
    processus voit.
    """
    import reviewer.repo.git as g

    vus = {}

    def faux_git(cwd, *args, check=True, env=None):
        vus["args"] = args
        vus["env"] = env or {}
        return "ok"

    monkeypatch.setattr(g, "_git", faux_git)
    monkeypatch.setattr(g, "current_branch", lambda _w: "feat/x")

    g.push(tmp_path, remote="origin", token="JETON-SECRET")

    assert not any("JETON-SECRET" in a for a in vus["args"]),         "le jeton ne doit pas passer par argv"
    assert not any("extraheader" in a for a in vus["args"])
    # Il est bien transmis, par l'autre voie.
    assert vus["env"].get("GIT_CONFIG_KEY_0") == "http.extraheader"
    assert "AUTHORIZATION: basic " in vus["env"].get("GIT_CONFIG_VALUE_0", "")
