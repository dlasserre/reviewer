"""La configuration tient-elle ses cinq promesses ?

Chaque test vise une regle de `config.py`, et surtout son cas DEFAVORABLE : une
configuration valide qui se charge ne prouve pas grand-chose, une configuration
fautive qui se charge quand meme est exactement le bug qu'on cherche a rendre
impossible.
"""

from __future__ import annotations

import textwrap

import pytest

from agent_runner_lg.config import (
    Access,
    ConfigError,
    SecretRef,
    load_profile,
    load_profiles,
    load_runner,
    parse_duration,
)
from agent_runner_lg.rules.machine import Severity

RUNNER_MINIMAL = """
worktrees_root: C:/tmp/wt
state_db: C:/tmp/state.db
logs_dir: C:/tmp/logs
"""

PROFIL_MINIMAL = """
project: demo
workspace: C:/tmp/demo
forge:
  org: UneOrg
"""


def ecrire(tmp_path, nom, contenu):
    f = tmp_path / nom
    f.write_text(textwrap.dedent(contenu), encoding="utf-8")
    return f


# ── Regle 3 : les defauts sont les valeurs sures ────────────────────────────


def test_les_ecritures_sont_desactivees_par_defaut(tmp_path):
    cfg = load_runner(ecrire(tmp_path, "runner.yaml", RUNNER_MINIMAL))
    assert cfg.writes_enabled is False


def test_un_depot_non_qualifie_est_en_lecture_seule(tmp_path):
    p = load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
repos:
  quelque-chose:
    path: C:/tmp/demo/x
"""))
    # Ne PAS nommer l'acces ne donne pas l'ecriture : c'est toute la these.
    assert p.repos["quelque-chose"].access is Access.CONTEXT
    assert p.writable == {}


def test_default_access_est_context(tmp_path):
    p = load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL))
    assert p.default_access is Access.CONTEXT


# `default_access` a longtemps ete DECLARE sans etre lu : le test ci-dessus
# passait pendant que le reglage ne gouvernait rien. Verifier une valeur de
# champ ne prouve rien sur le comportement — les trois suivants testent l'EFFET.


def test_default_access_gouverne_les_depots_sans_acces(tmp_path):
    p = load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
default_access: ignore
repos:
  muet:
    path: C:/tmp/demo/muet
"""))
    assert p.repos["muet"].access is Access.IGNORE
    assert p.writable == {}


def test_un_acces_explicite_survit_au_defaut(tmp_path):
    # Le defaut comble une ABSENCE ; il n'ecrase jamais un choix ecrit. Sinon
    # durcir `default_access` retirerait en silence des acces voulus.
    p = load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
default_access: ignore
repos:
  choisi:
    access: write
    path: C:/tmp/demo/choisi
    checks: [ "pytest -q" ]
"""))
    assert p.repos["choisi"].access is Access.WRITE


def test_durcir_le_defaut_ne_donne_jamais_l_ecriture(tmp_path):
    # Le sens de la these : un profil duplique pour un nouveau projet peut
    # partir ferme et s'ouvrir depot par depot. L'inverse — un defaut qui
    # DONNE l'ecriture a ce qui ne l'a pas demandee — est refuse.
    with pytest.raises(ConfigError):
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
default_access: write
repos:
  implicite:
    path: C:/tmp/demo/implicite
"""))


# ── Regle 2 : cle inconnue = erreur ─────────────────────────────────────────


def test_une_cle_inconnue_refuse_de_charger(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + "\nmax_review_cycle: 3\n"))
    # Le typo est `max_review_cycle` (sans `s`). S'il passait, la valeur reelle
    # resterait le defaut sans que personne ne le voie.
    assert "max_review_cycle" in str(e.value)


def test_un_acces_mal_orthographie_refuse_de_charger(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
repos:
  api:
    acces: write
    path: C:/tmp/demo/api
"""))
    assert "acces" in str(e.value)


def test_une_valeur_d_acces_inconnue_refuse_de_charger(tmp_path):
    with pytest.raises(ConfigError):
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
repos:
  api:
    access: read-write
    path: C:/tmp/demo/api
"""))


# ── Regle 1 : aucun secret en clair ─────────────────────────────────────────


def test_un_secret_en_clair_est_refuse(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(ecrire(tmp_path, "p.yaml", """
project: demo
workspace: C:/tmp/demo
forge:
  org: UneOrg
  token_write: ghp_unVraiJetonQuiNAurionsPasDuEtreLa
"""))
    assert "env:" in str(e.value)


def test_une_reference_env_est_acceptee_meme_si_la_variable_manque(tmp_path, monkeypatch):
    monkeypatch.delenv("PAT_WRITE", raising=False)
    p = load_profile(ecrire(tmp_path, "p.yaml", """
project: demo
workspace: C:/tmp/demo
forge:
  org: UneOrg
  token_write: env:PAT_WRITE
"""))
    # Valider n'exige pas d'avoir les secrets : on doit pouvoir relire le profil
    # d'un autre poste, ou le valider en CI.
    assert p.forge.token_write.var == "PAT_WRITE"


def test_la_resolution_leve_quand_la_variable_manque(monkeypatch):
    monkeypatch.delenv("ABSENTE", raising=False)
    with pytest.raises(ConfigError) as e:
        SecretRef("env:ABSENTE").resolve()
    assert "ABSENTE" in str(e.value)


def test_une_variable_vide_vaut_une_variable_absente(monkeypatch):
    monkeypatch.setenv("VIDE", "")
    with pytest.raises(ConfigError):
        SecretRef("env:VIDE").resolve()


def test_la_resolution_rend_la_valeur(monkeypatch):
    monkeypatch.setenv("PAT", "valeur-secrete")
    assert SecretRef("env:PAT").resolve() == "valeur-secrete"


# ── Le piege de facturation ─────────────────────────────────────────────────


def test_on_ne_peut_pas_retirer_la_garde_sur_la_cle_api(tmp_path):
    # `ANTHROPIC_API_KEY` prime sur `CLAUDE_CODE_OAUTH_TOKEN` : la laisser dans
    # l'environnement fait basculer la facturation de l'abonnement vers l'API,
    # en silence. La retirer de `scrub_env` doit etre impossible.
    with pytest.raises(ConfigError) as e:
        load_runner(ecrire(tmp_path, "runner.yaml", RUNNER_MINIMAL + """
claude:
  scrub_env: [ AUTRE_CHOSE ]
"""))
    assert "ANTHROPIC_API_KEY" in str(e.value)


# ── Modes de permission ─────────────────────────────────────────────────────


def test_bypass_permissions_est_refuse(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + "\npermission_mode: bypassPermissions\n"))
    assert "allowed_tools" in str(e.value)


def test_accept_edits_est_accepte(tmp_path):
    p = load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + "\npermission_mode: acceptEdits\n"))
    assert p.permission_mode == "acceptEdits"


# ── Branches de travail ─────────────────────────────────────────────────────


@pytest.mark.parametrize("branche", ["dev", "main", "master", "production"])
def test_une_branche_partagee_ne_peut_pas_etre_une_branche_de_travail(tmp_path, branche):
    with pytest.raises(ConfigError):
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + f"""
repos:
  api:
    access: write
    path: C:/tmp/demo/api
    branches: [ "feat/*", "{branche}" ]
"""))


# ── L'API locale ────────────────────────────────────────────────────────────


def test_l_api_refuse_d_ecouter_sur_toutes_les_interfaces(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_runner(ecrire(tmp_path, "runner.yaml", RUNNER_MINIMAL + """
api:
  bind: 0.0.0.0
"""))
    assert "boucle locale" in str(e.value)


# ── Durees ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("texte", "secondes"), [
    ("30s", 30.0), ("5m", 300.0), ("2h", 7200.0), (" 90 s ", 90.0), ("1.5m", 90.0),
])
def test_les_durees_se_lisent_avec_leur_unite(texte, secondes):
    assert parse_duration(texte) == secondes


def test_une_duree_sans_unite_est_refusee():
    # `poll_wait: 50` ne dit pas si c'est 50 secondes ou 50 minutes, et les deux
    # se defendent. On refuse plutot que de deviner.
    with pytest.raises(ValueError, match="sans unite"):
        parse_duration(50)


def test_une_duree_illisible_est_refusee():
    with pytest.raises(ValueError, match="illisible"):
        parse_duration("bientot")


# ── Fenetre de travail ──────────────────────────────────────────────────────


def test_une_fenetre_de_travail_est_refusee(tmp_path):
    # Retiree le 26/08/2026 : elle autorisait l'agent pile pendant les heures
    # de travail qu'elle pretendait proteger, et l'eteignait la nuit. La cle
    # doit desormais REFUSER de charger plutot que d'etre ignoree en silence —
    # sinon un profil qui la porte encore croit avoir une borne qui n'existe
    # plus.
    with pytest.raises(ConfigError):
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
budget:
  work_window: "07:00-23:00 Europe/Paris"
"""))


# ── Substitution {workspace} ────────────────────────────────────────────────


def test_workspace_est_substitue_dans_les_chemins_de_depot(tmp_path):
    p = load_profile(ecrire(tmp_path, "p.yaml", """
project: demo
workspace: C:/tmp/demo
forge:
  org: UneOrg
repos:
  api:
    access: write
    path: "{workspace}/api"
"""))
    assert str(p.repos["api"].path).replace("\\", "/") == "C:/tmp/demo/api"


# ── Regle 4 : un profil invalide n'emporte pas les autres ───────────────────


def test_un_profil_invalide_est_ecarte_mais_signale(tmp_path):
    d = tmp_path / "profils"
    d.mkdir()
    ecrire(d, "bon.yaml", PROFIL_MINIMAL)
    ecrire(d, "casse.yaml", "project: autre\nworkspace: C:/tmp/x\ncle_inconnue: 1\n")

    profils, erreurs = load_profiles(d)

    assert set(profils) == {"demo"}          # le bon profil tourne encore
    assert "casse.yaml" in erreurs           # et l'erreur n'est PAS avalee
    assert "cle_inconnue" in erreurs["casse.yaml"]


def test_deux_profils_du_meme_nom_ne_se_remplacent_pas_en_silence(tmp_path):
    d = tmp_path / "profils"
    d.mkdir()
    ecrire(d, "a.yaml", PROFIL_MINIMAL)
    ecrire(d, "b.yaml", PROFIL_MINIMAL)
    profils, erreurs = load_profiles(d)
    assert set(profils) == {"demo"}
    assert "b.yaml" in erreurs


def test_un_dossier_de_profils_vide_ne_leve_pas(tmp_path):
    d = tmp_path / "profils"
    d.mkdir()
    profils, erreurs = load_profiles(d)
    assert profils == {} and erreurs == {}


def test_un_dossier_de_profils_absent_leve(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_profiles(tmp_path / "nulle-part")


# ── Diagnostics ─────────────────────────────────────────────────────────────


def test_un_yaml_invalide_nomme_le_fichier(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_runner(ecrire(tmp_path, "runner.yaml", "worktrees_root: [ non ferme\n"))
    assert "runner.yaml" in str(e.value)


def test_un_fichier_vide_le_dit(tmp_path):
    with pytest.raises(ConfigError, match="vide"):
        load_runner(ecrire(tmp_path, "runner.yaml", "\n"))


def test_l_erreur_nomme_la_cle_fautive(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
repos:
  api:
    access: write
    path: C:/tmp/demo/api
    branches: [ "main" ]
"""))
    # Un message pydantic brut ne sert a rien a 2 h du matin : on veut le chemin
    # de la cle.
    assert "repos.api.branches" in str(e.value)


# ── Tri par acces ───────────────────────────────────────────────────────────


def test_les_depots_se_trient_par_acces(tmp_path):
    p = load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
repos:
  api:
    access: write
    path: "{workspace}/api"
  perso:
    access: context
    path: "{workspace}/perso"
  vieux:
    access: ignore
    path: "{workspace}/vieux"
"""))
    assert set(p.writable) == {"api"}
    assert set(p.repos_by_access(Access.CONTEXT)) == {"perso"}
    assert set(p.repos_by_access(Access.IGNORE)) == {"vieux"}


# ── Le moteur suit la gravite de la remarque ────────────────────────────────


def _profil_gradue(tmp_path):
    return load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
model: claude-sonnet-5
effort: medium
per_severity:
  P1:
    model: claude-opus-5
    effort: high
  P3:
    effort: low
"""))


def test_chaque_severite_prend_son_moteur(tmp_path):
    p = _profil_gradue(tmp_path)
    assert p.moteur(Severity.P1) == ("claude-opus-5", "high")
    assert p.moteur(Severity.P3) == ("claude-sonnet-5", "low")


def test_un_champ_absent_retombe_sur_le_global_SEPAREMENT(tmp_path):
    # L'entree P3 ne nomme que l'effort : le modele reste le global. Rendre le
    # couple d'un bloc obligerait a redire le modele dans chaque entree, donc a
    # le mettre a jour a quatre endroits le jour ou il change.
    p = _profil_gradue(tmp_path)
    assert p.moteur(Severity.P3)[0] == "claude-sonnet-5"


def test_une_severite_sans_entree_prend_le_global(tmp_path):
    p = _profil_gradue(tmp_path)
    assert p.moteur(Severity.P2) == ("claude-sonnet-5", "medium")
    assert p.moteur(Severity.UNKNOWN) == ("claude-sonnet-5", "medium")


def test_sans_severite_on_prend_le_global(tmp_path):
    # Un job declenche par une CI rouge n'a aucun fil, donc aucune severite.
    p = _profil_gradue(tmp_path)
    assert p.moteur(None) == ("claude-sonnet-5", "medium")


def test_une_severite_inconnue_refuse_de_charger(tmp_path):
    # Une faute de frappe ferait tourner le job au reglage global sans que rien
    # ne dise pourquoi celui qu'on croyait avoir pose ne s'applique jamais.
    with pytest.raises(ConfigError) as e:
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
per_severity:
  P4:
    effort: low
"""))
    assert "P4" in str(e.value)


def test_un_effort_invalide_est_refuse_AUSSI_par_severite(tmp_path):
    # Meme regle que le champ global, et litteralement la meme fonction : deux
    # jeux de validation sur un meme reglage finissent par diverger, et c'est
    # toujours le moins strict qui laisse passer.
    with pytest.raises(ConfigError) as e:
        load_profile(ecrire(tmp_path, "p.yaml", PROFIL_MINIMAL + """
per_severity:
  P1:
    effort: turbo
"""))
    assert "turbo" in str(e.value)
