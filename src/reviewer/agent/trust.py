"""Rendre exploitables les reglages VERSIONNES des depots de travail.

── LA PANNE QUE CE MODULE SUPPRIME ─────────────────────────────────────────

Le demon tourne avec un repertoire de configuration Claude DEDIE
(`claude.config_dir`), isole de celui de l'humain. C'est voulu : la memoire
automatique se charge independamment de `setting_sources`, et sans cet
isolement le contexte d'un projet polluerait celui d'un autre.

Mais un espace de configuration neuf n'a accepte le dialogue de confiance pour
aucun repertoire. Claude Code refuse alors d'appliquer ce qu'un depot declare
dans son `.claude/settings.json`, et le dit sur la sortie d'erreur :

    Ignoring 15 permissions.allow entries from .claude/settings.json:
    this workspace has not been trusted.

Mesure du 27/08/2026 sur `Insectorize/backend` : les 15 entrees ignorees ne
changeaient RIEN aux outils — en mode SDK, `permissions.allow` pre-approuve des
invites interactives que le demon n'a pas, et 0 refus a ete observe sur une
session reelle. Ce qui se perd, ce sont les HOOKS du depot : cinq y sont
declares, dont `router-safety.sh` et `mongo-safety.sh` sur chaque ecriture.

Autrement dit, le depot perd exactement ses garde-fous a l'instant ou un
automate ecrit dedans. Et la perte est silencieuse : un avertissement rouge de
plus, sur une sortie qui en affiche a chaque passage, finit par ne plus etre lu.

── POURQUOI CE N'EST PAS UN ELARGISSEMENT DE DROITS ────────────────────────

On ne fait confiance qu'aux repertoires que le profil declare DEJA en
`access: write`. La decision a donc ete prise en amont, explicitement, depot par
depot — ce module ne fait que la rendre effective la ou le SDK la lit. Un depot
`context` n'y entre jamais.

── CE QUE CE MODULE REFUSE DE FAIRE ────────────────────────────────────────

Ecrire dans un fichier qu'il n'a pas su relire. Claude Code tient ce fichier
lui-meme et le reecrit ; le reconstruire depuis une lecture partielle
effacerait des reglages qui ne nous appartiennent pas. Un JSON illisible est
donc RENDU comme un echec, jamais ecrase.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TrustReport", "project_key", "ensure_trusted", "untrusted"]

_FICHIER = ".claude.json"


def project_key(chemin: Path) -> str:
    """Forme sous laquelle Claude Code indexe un repertoire de projet.

    Des barres obliques, meme sous Windows — c'est ce qu'on lit dans le fichier
    reel (`C:/Users/damie/WebstormProjects/plantifia/backend`). Ecrire la forme
    Windows creerait une SECONDE entree pour le meme repertoire, et la confiance
    resterait sans effet : deux cles, dont une que personne ne consulte.
    """
    return str(Path(chemin).resolve()).replace("\\", "/")


def _comparable(cle: str) -> str:
    """Cle normalisee pour la COMPARAISON seulement.

    Les chemins Windows ne sont pas sensibles a la casse : une entree ecrite
    `C:/Users/...` et une autre `c:/users/...` designent le meme repertoire. Ne
    comparer que la forme exacte en ajouterait une troisieme a chaque variation.
    """
    cle = cle.replace("\\", "/").rstrip("/")
    return cle.lower() if sys.platform == "win32" else cle


@dataclass(frozen=True, slots=True)
class TrustReport:
    """Ce qui a ete fait, ou n'a pas pu l'etre. Rendu, jamais avale."""

    granted: tuple[str, ...] = ()        # repertoires nouvellement fiables
    already: tuple[str, ...] = ()        # deja fiables : rien a faire
    failed: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (quoi, pourquoi)

    @property
    def changed(self) -> bool:
        return bool(self.granted)

    def summary(self) -> str:
        morceaux = []
        if self.granted:
            morceaux.append(f"{len(self.granted)} repertoire(s) rendus fiables")
        if self.already:
            morceaux.append(f"{len(self.already)} deja fiable(s)")
        if self.failed:
            morceaux.append(f"{len(self.failed)} en echec")
        return ", ".join(morceaux) or "aucun repertoire a traiter"


def untrusted(config_dir: Path, chemins) -> tuple[str, ...]:
    """Repertoires qui ne sont PAS encore fiables. Lecture pure.

    Existe pour que `check` puisse le DIRE sans rien ecrire : une commande de
    diagnostic qui modifie l'etat qu'elle diagnostique n'est plus un
    diagnostic. Un fichier illisible rend tous les chemins comme non fiables —
    c'est la reponse prudente, et `ensure_trusted` refusera de l'ecraser.
    """
    fichier = Path(config_dir) / _FICHIER
    try:
        data = json.loads(fichier.read_text(encoding="utf-8"))
        projets = data["projects"]
        if not isinstance(projets, dict):
            raise TypeError
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return tuple(project_key(c) for c in chemins)

    fiables = {
        _comparable(k) for k, v in projets.items()
        if isinstance(v, dict) and v.get("hasTrustDialogAccepted") is True
    }
    return tuple(project_key(c) for c in chemins
                 if _comparable(project_key(c)) not in fiables)


def ensure_trusted(config_dir: Path, chemins) -> TrustReport:
    """Marque ces repertoires comme fiables dans la configuration du demon.

    Idempotent : rappele sur un fichier deja a jour, il n'ECRIT PAS. Ce n'est
    pas une optimisation — Claude Code tient ce meme fichier pendant qu'une
    session tourne, et chaque ecriture inutile est une occasion de perdre ce
    qu'il vient d'y mettre. On n'ecrit que s'il y a quelque chose a changer.

    L'ecriture est ATOMIQUE (`os.replace`) : une coupure laisse l'ancien
    fichier intact plutot qu'un fichier a moitie ecrit. Une configuration
    Claude tronquee se repare a la main, et personne n'a envie de ce moment.
    """
    chemins = [Path(c) for c in chemins]
    if not chemins:
        return TrustReport()

    fichier = Path(config_dir) / _FICHIER
    echecs: list[tuple[str, str]] = []

    if fichier.exists():
        try:
            data = json.loads(fichier.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # On ne reconstruit PAS un fichier qu'on n'a pas su relire : il
            # contient des reglages qui ne nous appartiennent pas.
            return TrustReport(failed=((str(fichier), f"illisible : {e}"),))
        if not isinstance(data, dict):
            return TrustReport(failed=((str(fichier), "racine JSON inattendue"),))
    else:
        # Premier demarrage : le fichier n'existe pas encore. On le cree avec le
        # strict minimum — Claude Code y ajoutera le reste lui-meme.
        data = {}

    projets = data.get("projects")
    if projets is None:
        projets = {}
        data["projects"] = projets
    elif not isinstance(projets, dict):
        return TrustReport(failed=((str(fichier), "« projects » n'est pas un objet"),))

    # Index des cles existantes par forme comparable, pour retrouver une entree
    # ecrite dans une autre casse plutot que d'en creer une seconde.
    connues = {_comparable(k): k for k in projets}

    accordes: list[str] = []
    deja: list[str] = []

    for chemin in chemins:
        cle = project_key(chemin)
        existante = connues.get(_comparable(cle))
        entree = projets.get(existante) if existante else None
        if not isinstance(entree, dict):
            entree = {}
        if entree.get("hasTrustDialogAccepted") is True:
            deja.append(cle)
            continue
        entree["hasTrustDialogAccepted"] = True
        projets[existante or cle] = entree
        accordes.append(cle)

    if not accordes:
        return TrustReport((), tuple(deja))

    try:
        fichier.parent.mkdir(parents=True, exist_ok=True)
        temporaire = fichier.with_suffix(fichier.suffix + ".agent-runner.tmp")
        temporaire.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                              encoding="utf-8")
        os.replace(temporaire, fichier)
    except OSError as e:
        echecs.append((str(fichier), f"ecriture impossible : {e}"))
        return TrustReport((), tuple(deja), tuple(echecs))

    return TrustReport(tuple(accordes), tuple(deja), tuple(echecs))
