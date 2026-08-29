"""Ce que le demon ECRIT sur la forge, en francais, pour un humain.

Sorti de `job.py` volontairement : ce sont les seuls textes du systeme qui
seront lus par quelqu'un d'autre que nous, et ils meritent d'etre relisibles et
testables sans lancer un agent ni toucher a GitHub.

── TROIS REGLES ────────────────────────────────────────────────────────────

1. DIRE OU EST LE CODE. Une reponse qui affirme « corrige » sans dire sur
   quelle branche ni dans quelle PR est invérifiable — et sur une PR de
   release, le correctif n'est meme pas sur la PR ou la reponse s'affiche.

2. LA MENTION EST LE DECLENCHEUR, PAS LA DECORATION. Elle n'apparait que quand
   une decision humaine est reellement attendue. Mentionner quelqu'un a chaque
   reponse apprend a ignorer la mention, et le jour ou elle compte, elle ne
   compte plus.

3. DIRE COMMENT RELANCER. Une question dont la reponse ne relance rien est une
   question posee dans le vide. Chaque appel a l'humain se termine donc par la
   facon de reprendre la main — repondre dans le fil.
"""

from __future__ import annotations

from agent_runner_lg.rules.machine import PullSnapshot, Severity, Thread
from agent_runner_lg.rules.verdict import Issue, ThreadVerdict, Verdict

__all__ = [
    "blocked_notice",
    "derived_issue_body",
    "derived_issue_title",
    "derived_pull_body",
    "derived_pull_title",
    "issue_marker",
    "thread_reply",
]


def issue_marker(repo: str, pr: int) -> str:
    """Marqueur invisible qui identifie l'issue d'une PR relue.

    Sert de garde anti-doublon quand l'etat local a disparu. Il porte le depot
    ET le numero : deux PR de release differentes ne doivent pas se reconnaitre
    l'une dans l'autre.
    """
    return f"<!-- agent-runner:revue {repo}#{pr} -->"

_RELANCE = (
    "_Repondre dans ce fil relance l'agent : votre message devient le dernier "
    "mot, et le travail reprend au passage suivant._"
)


def _ou_est_le_code(*, branch: str | None, pull_url: str | None,
                    derived: bool) -> str:
    """Une phrase qui situe le correctif. Vide si rien n'a ete pousse."""
    if not branch:
        return ""
    if derived and pull_url:
        return (f"Le correctif est sur `{branch}` et arrive par une PR "
                f"distincte : {pull_url}")
    if derived:
        # Pousse mais pas encore de PR : le dire plutot que de laisser croire
        # que le correctif est deja soumis quelque part.
        return (f"Le correctif est pousse sur `{branch}`. La PR qui le porte "
                "n'a pas encore pu etre ouverte.")
    return f"Le correctif est pousse sur `{branch}`."


def thread_reply(
    verdict: ThreadVerdict,
    *,
    notify: str | None = None,
    branch: str | None = None,
    pull_url: str | None = None,
    derived: bool = False,
) -> str:
    """Reponse publiee dans un fil de revue.

    Le texte de l'agent est repris TEL QUEL — c'est lui qui a lu le code. On
    n'ajoute que ce qu'il ne pouvait pas savoir : ou son travail a atterri, et
    ce qui se passe ensuite.
    """
    lignes = [verdict.reply.strip() or "_(aucune explication rendue par l'agent)_"]

    if verdict.outcome is Issue.CORRIGE:
        if situe := _ou_est_le_code(branch=branch, pull_url=pull_url, derived=derived):
            lignes += ["", situe]
        return "\n".join(lignes)

    # REFUTE et ARBITRAGE laissent le fil OUVERT et appellent l'humain. La
    # distinction est dans la phrase, pas dans le mecanisme : dans les deux cas
    # la decision revient a quelqu'un.
    if verdict.outcome is Issue.REFUTE:
        entete = "**Desaccord avec cette remarque — le fil reste ouvert.**"
    else:
        entete = "**Cette remarque demande un arbitrage — le fil reste ouvert.**"
    lignes.insert(0, entete)
    lignes.insert(1, "")

    if situe := _ou_est_le_code(branch=branch, pull_url=pull_url, derived=derived):
        lignes += ["", situe]
    if notify:
        lignes += ["", f"{notify} — a toi de trancher."]
    lignes += ["", _RELANCE]
    return "\n".join(lignes)


def thread_unanswered(
    raison: str,
    *,
    notify: str | None = None,
    branch: str | None = None,
    pull_url: str | None = None,
    derived: bool = False,
) -> str:
    """Reponse dans un fil que l'agent n'a PAS traite.

    ── LE SILENCE EST LE PIRE DES ETATS ────────────────────────────────────

    Deux chemins laissaient un fil sans un mot : un verdict qui ne le mentionne
    pas (`for_thread` rend `None`), et un job qui s'arrete avant de publier —
    CI rouge, worktree impossible, agent qui n'a pas conclu.

    Dans les deux cas la remarque restait ouverte, sans reponse, et rien ne
    disait si elle avait ete vue. Une remarque ignoree ressemble exactement a
    une remarque perdue, et c'est la personne qui l'a ecrite qui paie la
    difference — elle relance, ou elle abandonne.

    Le fil reste OUVERT : on ne solde pas ce qu'on n'a pas traite.

    ── DEUX SITUATIONS, DEUX PHRASES ───────────────────────────────────────

    « Cette remarque n'a pas ete traitee » suivi de « le correctif est pousse
    sur telle branche » est une contradiction, et elle a ete lue telle quelle
    le 29/08/2026 : on ne sait plus s'il y a du code a regarder ou non.

    Quand une branche existe, le travail a bien eu lieu — c'est sa VALIDATION
    qui n'a pas abouti. Le dire ainsi change ce qu'on va faire : relire un
    correctif, ou reprendre une remarque de zero.
    """
    entete = ("**Correctif ecrit, mais NON VALIDE — le fil reste ouvert.**"
              if branch else
              "**Cette remarque n'a pas ete traitee — le fil reste ouvert.**")
    lignes = [
        entete,
        "",
        raison.strip() or "L'agent n'a rien rendu sur ce point.",
    ]
    if situe := _ou_est_le_code(branch=branch, pull_url=pull_url, derived=derived):
        lignes += ["", situe]
    if notify:
        # Dire QUOI faire, pas seulement que c'est a faire. « A reprendre a la
        # main » ne dit pas par ou commencer, et le premier reflexe — relire le
        # code livre — est le mauvais quand ce sont les verifications qui n'ont
        # pas pu demarrer.
        quoi = ("relire le correctif, puis relancer les verifications" if branch
                else "reprendre cette remarque")
        lignes += ["", f"{notify} — {quoi}."]
    lignes += ["", _RELANCE]
    return "\n".join(lignes)


def blocked_notice(
    reason: str,
    *,
    repo: str,
    pr: int,
    notify: str | None = None,
    review_cycle: int = 0,
    max_review_cycles: int = 3,
    anomalies: tuple[str, ...] = (),
    extrait: str = "",
) -> str:
    """Commentaire de PR quand l'agent s'arrete sans pouvoir viser un fil.

    Une CI rouge, un worktree impossible a monter, un verdict illisible :
    aucun de ces cas n'appartient a une remarque de revue, donc aucun n'a de
    fil ou repondre. Sans cette voie, l'echec le plus frequent serait aussi le
    plus silencieux.

    `extrait` porte la FIN de la sortie de la commande qui a echoue. Sans lui,
    le commentaire disait « ECHEC (code 2) » et laissait deviner : il a fallu
    rejouer la commande a la main pour decouvrir que la collecte pytest
    echouait sur un `.env` absent, et que le code livre n'y etait pour rien.
    Ce que la commande a dit est la seule chose qui permette de trancher entre
    « le correctif est mauvais » et « l'environnement est incomplet ».
    """
    lignes = [
        f"**L'agent s'est arrete sur {repo}#{pr}.**",
        "",
        reason.strip(),
        "",
    ]
    if extrait.strip():
        # Replie : la sortie d'un outil est longue, et elle ne doit pas
        # repousser hors de vue la phrase qui dit quoi faire.
        lignes += [
            "<details><summary>Fin de la sortie de la commande</summary>",
            "",
            "```",
            extrait.strip(),
            "```",
            "</details>",
            "",
        ]
    lignes += [
        f"Cycle {review_cycle} sur {max_review_cycles} autorises.",
    ]
    if anomalies:
        # Journalisees ET publiees : une anomalie de format qu'on ne voit que
        # dans un JSONL local est une anomalie que personne ne verra.
        lignes += ["", "Anomalies relevees en lisant le verdict de l'agent :", ""]
        lignes += [f"- {a}" for a in anomalies[:8]]
    if notify:
        lignes += ["", f"{notify} — il faut un coup d'oeil."]
    lignes += [
        "",
        "_Repondre a ce commentaire ne relance rien : c'est un fil de revue qu'il "
        "faut alimenter, ou un nouveau commit sur la branche._",
    ]
    return "\n".join(lignes)


def derived_issue_title(pull: PullSnapshot) -> str:
    return f"Retours de revue sur la PR #{pull.number}"


def derived_issue_body(pull: PullSnapshot,
                       threads: tuple[Thread, ...] | list[Thread]) -> str:
    """Corps de l'issue qui porte les correctifs derives d'une PR d'integration.

    ── POURQUOI UNE ISSUE NEUVE, ET PAS CELLE D'ORIGINE ────────────────────

    Les remarques d'une PR d'integration portent sur du code DEJA fusionne,
    venu de plusieurs issues differentes et deja soldees. Rouvrir l'une d'elles
    serait arbitraire — laquelle ? — et ferait reculer une carte dont le code
    est dans `dev`. Ce sont des defauts trouves tard : du travail neuf.

    ── CE QUE CE CORPS NE CONTIENT PAS ────────────────────────────────────

    Le texte integral des remarques. Elles sont ecrites par un relecteur
    automatique a partir du contenu du depot, dependances comprises : on en
    donne un EXTRAIT normalise, et le fil d'origine reste la source. Recopier
    des milliers de caracteres de texte tiers dans un suivi n'aide personne et
    y publie ce qu'on n'a pas relu.
    """
    threads = tuple(threads)
    lignes = [
        issue_marker(pull.repo, pull.number),
        "",
        f"La relecture de la PR #{pull.number} a ouvert {len(threads)} fil(s) "
        "portant sur du code deja present dans l'integration.",
        "",
        "Cette PR ayant une branche partagee pour tete, on ne peut pas y "
        "commiter : le correctif arrive par une PR distincte, rattachee a "
        "cette issue.",
        "",
        f"## {len(threads)} remarque(s)",
        "",
    ]
    for t in threads:
        ou = f"`{t.path}:{t.line}`" if t.path else "_sans ancrage_"
        gravite = t.severity.name if t.severity is not Severity.UNKNOWN else "non etiquetee"
        extrait = " ".join(t.body.split())[:280]
        lignes.append(f"- {ou} — {gravite} — {extrait}")
    lignes += ["", f"Fils d'origine sur la PR #{pull.number}."]
    return "\n".join(lignes)


def derived_pull_title(pull: PullSnapshot) -> str:
    return f"fix({pull.repo}): retours de revue de #{pull.number}"


def derived_pull_body(
    pull: PullSnapshot,
    threads: tuple[Thread, ...] | list[Thread],
    verdict: Verdict,
    *,
    base: str,
    issue: int | None = None,
) -> str:
    """Corps de la PR derivee, quand la PR relue a pour tete une branche partagee.

    ── LE SEUL « Fixes # » AUTORISE ICI ────────────────────────────────────

    `Fixes #<n>` est le mot-cle qui lie l'issue et fait avancer la carte du
    Project. Il ne doit donc apparaitre QUE sur l'issue que cette PR solde
    reellement — jamais avec le numero de la PR relue, qui ferait bouger la
    carte d'une livraison entiere.

    Sans issue (`issues.enabled` a faux), aucun mot-cle de liaison n'est ecrit :
    on renvoie vers la PR d'origine en toutes lettres, ce qui informe sans rien
    declencher.
    """
    lignes = [
        f"Correctifs demandes par la relecture de la PR #{pull.number}.",
        "",
    ]
    if issue:
        lignes += [f"Fixes #{issue}", ""]
    lignes += [
        f"Cette PR vise `{base}`. La PR #{pull.number} a `{base}` pour tete : "
        "on ne peut pas y commiter sans ecrire directement sur l'integration, "
        "d'ou une PR distincte.",
        "",
    ]
    if verdict.summary:
        lignes += ["## Ce qui a change", "", verdict.summary.strip(), ""]

    traites = [t for t in verdict.threads if t.outcome is Issue.CORRIGE]
    restants = [t for t in verdict.threads if t.outcome is not Issue.CORRIGE]
    ancres = {t.id: t for t in threads}

    if traites:
        lignes += [f"## {len(traites)} remarque(s) traitee(s)", ""]
        for v in traites:
            t = ancres.get(v.thread_id)
            ou = f"`{t.path}:{t.line}`" if t is not None and t.path else "_sans ancrage_"
            lignes.append(f"- {ou} — {' '.join(v.reply.split())[:200]}")
        lignes.append("")
    if restants:
        lignes += [
            f"## {len(restants)} remarque(s) laissee(s) ouverte(s)", "",
            "Elles attendent une decision humaine, et leurs fils restent "
            f"ouverts sur #{pull.number}.", "",
        ]
        for v in restants:
            t = ancres.get(v.thread_id)
            ou = f"`{t.path}:{t.line}`" if t is not None and t.path else "_sans ancrage_"
            lignes.append(f"- {ou} — {v.outcome.value} — {' '.join(v.reply.split())[:200]}")
        lignes.append("")
    return "\n".join(lignes)
