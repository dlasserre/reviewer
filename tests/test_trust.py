"""La confiance des espaces de travail est-elle posee sans rien casser ?

Ce module ecrit dans un fichier que Claude Code tient LUI-MEME et reecrit
pendant qu'une session tourne. Deux proprietes comptent donc plus que le reste,
et ce sont elles qui sont testees en premier :

  - ne PAS ecrire quand il n'y a rien a changer, pour ne pas perdre ce que
    l'autre ecrivain vient d'y mettre ;
  - ne JAMAIS reconstruire un fichier qu'on n'a pas su relire — il contient des
    reglages qui ne nous appartiennent pas.

Le contexte : sans cette confiance, Claude Code ignore le
`.claude/settings.json` du depot, HOOKS COMPRIS. Le depot perd ses garde-fous a
l'instant precis ou un automate ecrit dedans (mesure du 27/08/2026 sur
`Insectorize/backend` : cinq hooks declares, dont deux sur chaque ecriture).
"""

from __future__ import annotations

import json
import sys

import pytest

from agent_runner_lg.agent.trust import ensure_trusted, project_key, untrusted


def lire(dossier):
    return json.loads((dossier / ".claude.json").read_text(encoding="utf-8"))


def ecrire(dossier, contenu):
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / ".claude.json").write_text(
        contenu if isinstance(contenu, str) else json.dumps(contenu, indent=2),
        encoding="utf-8")


# ── La forme de la cle ──────────────────────────────────────────────────────


def test_la_cle_utilise_des_barres_OBLIQUES_meme_sous_windows(tmp_path):
    # C'est la forme qu'on lit dans le fichier reel. Ecrire la forme Windows
    # creerait une SECONDE entree pour le meme repertoire, et la confiance
    # resterait sans effet : deux cles, dont une que personne ne consulte.
    assert "\\" not in project_key(tmp_path)
    assert project_key(tmp_path).endswith(tmp_path.name)


# ── Le chemin nominal ──────────────────────────────────────────────────────


def test_un_repertoire_inconnu_devient_fiable(tmp_path):
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, {"projects": {}})

    r = ensure_trusted(cfg, [depot])

    assert r.granted == (project_key(depot),)
    assert lire(cfg)["projects"][project_key(depot)]["hasTrustDialogAccepted"] is True


def test_une_entree_EXISTANTE_a_false_est_corrigee_sans_etre_remplacee(tmp_path):
    # Le cas reel : Claude Code avait deja enregistre le projet, avec ses
    # propres reglages, et `hasTrustDialogAccepted: false`. Recreer l'entree
    # effacerait tout ce qu'il y avait mis.
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, {"projects": {project_key(depot): {
        "hasTrustDialogAccepted": False,
        "history": ["une session"],
        "mcpServers": {"un": {}},
    }}})

    ensure_trusted(cfg, [depot])

    entree = lire(cfg)["projects"][project_key(depot)]
    assert entree["hasTrustDialogAccepted"] is True
    assert entree["history"] == ["une session"], "le reste de l'entree est intact"
    assert entree["mcpServers"] == {"un": {}}


def test_le_RESTE_du_fichier_n_est_pas_touche(tmp_path):
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, {"projects": {}, "userID": "abc", "tipsHistory": {"x": 3},
                 "oauthAccount": {"emailAddress": "x@y.z"}})

    ensure_trusted(cfg, [depot])

    apres = lire(cfg)
    assert apres["userID"] == "abc"
    assert apres["tipsHistory"] == {"x": 3}
    assert apres["oauthAccount"] == {"emailAddress": "x@y.z"}


def test_le_fichier_ABSENT_est_cree(tmp_path):
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    r = ensure_trusted(cfg, [depot])
    assert r.granted
    assert lire(cfg)["projects"][project_key(depot)]["hasTrustDialogAccepted"] is True


# ── Idempotence : ne pas ecrire pour rien ──────────────────────────────────


def test_un_repertoire_DEJA_fiable_ne_declenche_AUCUNE_ecriture(tmp_path):
    # Ce n'est pas une optimisation. Claude Code tient ce meme fichier pendant
    # qu'une session tourne : chaque ecriture inutile est une occasion de perdre
    # ce qu'il vient d'y mettre.
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, {"projects": {project_key(depot): {"hasTrustDialogAccepted": True}}})
    avant = (cfg / ".claude.json").stat().st_mtime_ns

    r = ensure_trusted(cfg, [depot])

    assert r.granted == () and r.already == (project_key(depot),)
    assert (cfg / ".claude.json").stat().st_mtime_ns == avant, "fichier reecrit pour rien"


def test_aucun_chemin_a_traiter_ne_touche_a_rien(tmp_path):
    cfg = tmp_path / "cfg"
    r = ensure_trusted(cfg, [])
    assert not r.changed
    assert not (cfg / ".claude.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="casse insensible : Windows")
def test_une_CASSE_differente_ne_cree_pas_une_SECONDE_entree(tmp_path):
    # Les chemins Windows ne sont pas sensibles a la casse. Deux entrees pour le
    # meme repertoire, c'est une confiance posee sur celle que personne ne lit.
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, {"projects": {project_key(depot).upper(): {
        "hasTrustDialogAccepted": False}}})

    ensure_trusted(cfg, [depot])

    projets = lire(cfg)["projects"]
    assert len(projets) == 1, projets
    assert next(iter(projets.values()))["hasTrustDialogAccepted"] is True


# ── Ce qu'on refuse de faire ───────────────────────────────────────────────


def test_un_fichier_ILLISIBLE_n_est_PAS_ecrase(tmp_path):
    # Il contient des reglages qui ne nous appartiennent pas. Le reconstruire
    # depuis une lecture ratee les effacerait — et une configuration Claude
    # perdue se repare a la main.
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, "{ceci n'est pas du JSON")

    r = ensure_trusted(cfg, [depot])

    assert r.failed and "illisible" in r.failed[0][1]
    assert r.granted == ()
    assert (cfg / ".claude.json").read_text(encoding="utf-8") == "{ceci n'est pas du JSON"


def test_un_projects_QUI_N_EST_PAS_UN_OBJET_est_refuse(tmp_path):
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, {"projects": ["pas", "un", "objet"]})

    r = ensure_trusted(cfg, [depot])

    assert r.failed and r.granted == ()
    assert lire(cfg)["projects"] == ["pas", "un", "objet"]


def test_aucun_fichier_temporaire_ne_reste(tmp_path):
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ensure_trusted(cfg, [depot])
    assert [p.name for p in cfg.iterdir()] == [".claude.json"]


# ── La lecture pure, pour `check` ──────────────────────────────────────────


def test_untrusted_ne_MODIFIE_rien(tmp_path):
    # Une commande de diagnostic qui modifie l'etat qu'elle diagnostique n'est
    # plus un diagnostic.
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, {"projects": {}})
    avant = (cfg / ".claude.json").read_text(encoding="utf-8")

    assert untrusted(cfg, [depot]) == (project_key(depot),)
    assert (cfg / ".claude.json").read_text(encoding="utf-8") == avant


def test_untrusted_ne_rend_que_ce_qui_MANQUE(tmp_path):
    cfg = tmp_path / "cfg"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    ecrire(cfg, {"projects": {project_key(a): {"hasTrustDialogAccepted": True}}})
    assert untrusted(cfg, [a, b]) == (project_key(b),)


def test_un_fichier_illisible_rend_TOUT_comme_non_fiable(tmp_path):
    # Reponse prudente : on ne suppose pas une confiance qu'on n'a pas pu lire.
    cfg, depot = tmp_path / "cfg", tmp_path / "depot"
    depot.mkdir()
    ecrire(cfg, "pas du JSON")
    assert untrusted(cfg, [depot]) == (project_key(depot),)
