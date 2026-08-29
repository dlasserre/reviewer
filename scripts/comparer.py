"""Les deux arbres portent-ils bien les MEMES REGLES ?

── CE QUE CE SCRIPT MESURE, ET CE QU'IL NE MESURE PAS ──────────────────────

Premiere version de ce script : lancer `status` des deux cotes et comparer les
etats affiches. Elle mesurait mal, pour deux raisons decouvertes en la lancant.

1. LES DEUX LECTURES N'ONT PAS LIEU AU MEME INSTANT. Sur `backend#727`, la CI
   tournait : un lancement a rendu `origine=WAITING_REVIEW, portage=WAITING_CI`,
   le suivant exactement l'INVERSE. L'ecart s'inversait avec l'ordre de lecture
   — c'etait la PR qui bougeait, pas une regle qui differait. Une comparaison
   dont le resultat depend de qui lit en premier ne compare rien.

2. `machine.py` EST IDENTIQUE dans les deux arbres, au caractere pres. Il
   n'importe rien du paquet — seulement `re`, `dataclasses`, `datetime`, `enum`
   — donc le portage l'a copie sans le toucher. Comparer deux `decide` qui sont
   le meme code sur des donnees differentes ne pouvait produire que du bruit.

Ce script fait donc l'inverse : il lit la forge UNE FOIS, et donne le MEME
snapshot aux deux implementations. La seule variable qui reste est le code des
regles.

── CE QU'IL ATTRAPE ────────────────────────────────────────────────────────

Une divergence signifie qu'un des deux arbres a ete modifie sans l'autre — une
correction appliquee d'un seul cote, une regle qui a derive. C'est exactement ce
qui rend un portage dangereux : deux systemes qu'on croit jumeaux et qui ne le
sont plus.

Il ne prouve PAS l'equivalence de comportement. Le seul morceau reellement
reecrit est l'orchestration (`job.py` + `reconcile.py` -> le graphe), et ce qui
en repond, ce sont les tests — pas ce script. Une equivalence de bout en bout
demanderait de lancer deux agents sur les memes fils, donc de publier deux fois.

Usage :

    .venv/Scripts/python.exe scripts/comparer.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent.parent
ORIGINE = ICI.parent / "claude-agent-runner"

# Les deux paquets cohabitent dans ce processus, et c'est sans danger PARCE QUE
# `machine.py` ne depend de rien : ni configuration, ni base d'etat, ni reseau.
# Importer deux `config` ou deux `StateStore` melangerait au contraire deux
# installations qui ne doivent pas se croiser.
sys.path.insert(0, str(ICI / "src"))
sys.path.insert(0, str(ORIGINE / "src"))


async def lire_une_fois():
    """Une seule lecture de la forge, partagee par les deux implementations."""
    from agent_runner_lg.__main__ import _token
    from agent_runner_lg.config import Access, load_profiles, load_runner
    from agent_runner_lg.forge.reader import GitHubReader
    from agent_runner_lg.rules.machine import compile_ignored

    load_runner(ICI / "runner.yaml")
    profils, _ = load_profiles(ICI / "profils")

    lots = []
    for profile in profils.values():
        if not profile.repos_by_access(Access.WRITE):
            continue
        async with GitHubReader(
                profile.forge.org, _token(profile),
                ignored_checks=compile_ignored(profile.ignored_checks)) as r:
            for nom, depot in profile.repos_by_access(Access.WRITE).items():
                lots.append((profile, nom, await r.open_pulls(nom)))
    return lots


def main() -> int:
    from agent_runner import machine as origine
    from agent_runner_lg.rules import machine as portage

    lots = asyncio.run(lire_une_fois())
    total = ecarts = 0

    for profile, _depot, pulls in lots:
        trust = frozenset(profile.reviewers.trust)
        ignores = portage.compile_ignored(profile.ignored_checks)
        # `now` est un PARAMETRE des deux cotes : le figer est ce qui rend la
        # comparaison reproductible. Le laisser flotter reintroduirait
        # exactement la course qu'on vient de retirer — la fenetre de revue se
        # referme entre deux appels.
        maintenant = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)

        for pull in pulls:
            total += 1
            commun = dict(trusted_reviewers=trust,
                          max_review_cycles=profile.max_review_cycles,
                          review_window_s=profile.reviewers.nudge_after,
                          nudge_enabled=bool(profile.reviewers.nudge_comment),
                          ignored_checks=ignores, now=maintenant)
            a = portage.decide(pull, **commun)
            # Le snapshot vient du portage ; les deux `PullSnapshot` sont des
            # dataclasses de meme forme, et `decide` ne lit que des attributs.
            b = origine.decide(pull, **commun)

            meme = (a.state.value == b.state.value
                    and a.reason == b.reason
                    and [x.value for x in a.actions] == [x.value for x in b.actions]
                    and [t.id for t in a.threads] == [t.id for t in b.threads])
            if meme:
                print(f"  OK {pull.repo}#{pull.number:<6} {a.state.value}")
                continue
            ecarts += 1
            print(f"  !! {pull.repo}#{pull.number:<6} LES REGLES DIVERGENT")
            print(f"        origine : {b.state.value} — {b.reason}")
            print(f"        portage : {a.state.value} — {a.reason}")

    print()
    print(f"{total} PR, meme lecture, memes parametres.")
    if ecarts:
        print(f"{ecarts} divergence(s) de REGLE : un arbre a ete modifie sans "
              "l'autre. A regler avant d'armer quoi que ce soit.")
        return 1
    print("Aucune divergence : les deux arbres portent les memes regles.")
    print("\nRappel : ce script ne dit RIEN de l'orchestration, qui est le seul")
    print("morceau reellement reecrit. Ce qui en repond, c'est `pytest`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
