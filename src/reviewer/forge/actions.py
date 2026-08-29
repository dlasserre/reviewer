"""Les gestes COMPOSES sur la forge : ouvrir une issue, une PR, repondre, alerter.

`writer.py` porte les appels ELEMENTAIRES — un appel d'API, un objet rendu.
Ici, ce sont les gestes qui en enchainent plusieurs et decident quoi ecrire :
retrouver ou creer l'issue de rattachement, ouvrir la PR derivee, repondre fil
par fil, prevenir quelqu'un quand on s'arrete.

── POURQUOI CE MODULE, ET PAS DANS LES NOEUDS ──────────────────────────────

Un noeud du graphe doit se lire en vingt lignes. `assurer_issue` en fait
soixante a elle seule, et son interet est ailleurs : trois niveaux
d'idempotence, un echec qui degrade sans faire perdre le correctif. Ce sont des
regles d'ECRITURE SUR LA FORGE, pas d'orchestration.

── DES FONCTIONS, PAS UNE CLASSE ───────────────────────────────────────────

Chacune recoit explicitement ce dont elle a besoin. Un objet qui porterait
`writer`, `journal`, `store` et `profile` rendrait invisible le fait que
`publier_verdict` n'ecrit RIEN en base et que `assurer_issue`, si.

── AUCUNE NE LEVE ────────────────────────────────────────────────────────────

Toutes rattrapent `ForgeError` et le journalisent. La raison est constante : a
ce stade, le travail de l'agent EXISTE — un correctif ecrit, souvent pousse. Le
perdre parce qu'une creation de PR a echoue serait un mauvais echange. On
degrade, on le dit fort, et le cycle suivant reprend.
"""

from __future__ import annotations

from dataclasses import replace

from reviewer.config import ProfileConfig
from reviewer.forge.base import ForgeError
from reviewer.output import report
from reviewer.output.events import Event, Journal
from reviewer.rules.machine import Decision, PullSnapshot, Thread
from reviewer.rules.verdict import Verdict
from reviewer.store.leases import PullState, StateStore

__all__ = [
    "assurer_issue",
    "classer_issue",
    "ouvrir_pr",
    "prevenir_humain",
    "publier_verdict",
    "signaler_arret",
]


async def assurer_issue(
    *,
    writer,
    journal: Journal,
    store: StateStore,
    profile: ProfileConfig,
    repo: str,
    snap: PullSnapshot,
    threads: tuple[Thread, ...],
    etat: PullState,
) -> int | None:
    """Trouve ou cree l'issue qui portera les correctifs derives.

    ── POURQUOI SEULEMENT POUR UNE PR DERIVEE ──────────────────────────────

    Une PR ordinaire est DEJA rattachee a son issue : son correctif y retourne,
    et en creer une seconde dedoublerait le suivi. Une PR d'integration, elle,
    ne porte l'issue de personne — ses remarques visent du code venu de
    plusieurs issues deja soldees. Le correctif est du travail neuf, et il lui
    faut son propre rattachement.

    ── TROIS NIVEAUX D'IDEMPOTENCE, DANS CET ORDRE ─────────────────────────

    1. L'ETAT LOCAL. Cle principale, parce qu'elle est immediate.
    2. LA RECHERCHE PAR MARQUEUR. Filet quand la base a disparu — et c'est
       aussi la « recherche de doublons avant creation » que le depot demande.
       Elle ne peut pas etre la cle principale : l'index de GitHub met des
       dizaines de secondes a voir une issue neuve, donc deux passages
       rapproches en creeraient deux.
    3. RIEN. On cree.

    Ne leve JAMAIS. Une issue qu'on n'a pas pu creer degrade le rattachement ;
    elle ne doit pas faire perdre le correctif.
    """
    if not profile.issues.enabled or writer is None:
        return None
    if etat.derived_issue:
        return etat.derived_issue

    marqueur = report.issue_marker(repo, snap.number)
    try:
        if existante := await writer.find_issue_by_marker(repo, marqueur):
            numero = int(existante.get("number") or 0)
            if numero:
                journal.emit(Event(
                    event="job.issue_reused", profile=profile.project,
                    repository=repo, pull_request=snap.number,
                    why=f"issue #{numero} deja ouverte pour cette revue "
                        "(retrouvee par marqueur, etat local absent)",
                ))
                store.save_pull_state(replace(etat, derived_issue=numero))
                return numero

        creee = await writer.create_issue(
            repo,
            title=report.derived_issue_title(snap),
            body=report.derived_issue_body(snap, threads),
            labels=profile.repos[repo].labels,
            assignee=profile.issues.assignee,
        )
    except ForgeError as e:
        # DEGRADE, pas fatal : le correctif partira sous un nom de branche de
        # repli et sans rattachement. Le dire fort, parce qu'une issue manquante
        # se voit mal — la PR existe, elle a l'air normale.
        journal.emit(Event(
            event="job.issue_failed", profile=profile.project,
            repository=repo, pull_request=snap.number,
            why=f"issue non creee ({e}) : le correctif partira sans "
                "rattachement, a lier a la main",
        ))
        return None

    numero = int((creee or {}).get("number") or 0)
    if not numero:
        return None
    store.save_pull_state(replace(etat, derived_issue=numero))
    await classer_issue(writer=writer, journal=journal, profile=profile,
                        repo=repo, numero=numero, threads=threads)
    journal.emit(Event(
        event="job.issue_created", profile=profile.project,
        repository=repo, pull_request=snap.number,
        why=f"issue #{numero} ouverte pour porter les correctifs de "
            f"{repo}#{snap.number}",
        detail={"url": (creee or {}).get("html_url")},
    ))
    return numero


async def classer_issue(
    *,
    writer,
    journal: Journal,
    profile: ProfileConfig,
    repo: str,
    numero: int,
    threads: tuple[Thread, ...],
) -> None:
    """Pose le type et la priorite. AU MIEUX, jamais bloquant.

    Ces deux champs sont natifs a l'issue et definis au niveau de
    l'ORGANISATION. Un PAT fine-grained recoit 403 sur leur catalogue (mesure du
    27/08/2026), et rien ne garantit que l'ecriture passe davantage. Echouer ici
    ne doit pas couter le correctif : l'issue existe, elle est liee, et un champ
    de classement se remplit a la main en deux secondes.

    La priorite se lit dans la SEVERITE des remarques, la seule donnee objective
    qu'on ait. `Severity` est ordonnee du plus grave au moins grave, `UNKNOWN`
    valant 99 : `min` rend donc la plus severe, et une remarque qu'on n'a pas su
    classer ne tire pas la priorite vers le bas.
    """
    reglages = profile.issues
    if reglages.type:
        try:
            await writer.set_issue_type(repo, numero, reglages.type)
        except ForgeError as e:
            journal.emit(Event(
                event="job.issue_field_failed", profile=profile.project,
                repository=repo,
                why=f"type « {reglages.type} » non pose sur #{numero} : {e}",
            ))
    if reglages.priority_field and threads:
        pire = min(t.severity for t in threads)
        valeur = reglages.priority_by_severity.get(pire.name)
        if not valeur:
            return
        try:
            await writer.set_issue_priority(
                repo, numero, field_id=reglages.priority_field, value=valeur)
        except ForgeError as e:
            journal.emit(Event(
                event="job.issue_field_failed", profile=profile.project,
                repository=repo,
                why=f"priorite « {valeur} » non posee sur #{numero} : {e}",
            ))


async def ouvrir_pr(
    *,
    writer,
    journal: Journal,
    profile: ProfileConfig,
    repo: str,
    snap: PullSnapshot,
    threads: tuple[Thread, ...],
    verdict: Verdict,
    branche: str,
    socle: str,
    issue: int | None = None,
) -> str | None:
    """Ouvre la PR qui porte le correctif derive. Rend son URL.

    Un echec ici n'annule PAS le travail : le code est pousse, il existe, et une
    PR s'ouvre a la main en dix secondes. On le journalise et on continue —
    perdre un cycle d'agent parce qu'une creation de PR a echoue serait un
    mauvais echange.
    """
    if writer is None:
        return None
    try:
        pull = await writer.create_pull(
            repo, head=branche, base=socle,
            title=report.derived_pull_title(snap),
            body=report.derived_pull_body(snap, threads, verdict,
                                          base=socle, issue=issue),
        )
    except ForgeError as e:
        journal.emit(Event(
            event="job.pull_failed", profile=profile.project,
            repository=repo, pull_request=snap.number,
            why=f"correctif pousse sur « {branche} » mais PR non ouverte : {e}",
        ))
        return None
    return (pull or {}).get("html_url")


async def publier_verdict(
    *,
    writer,
    journal: Journal,
    profile: ProfileConfig,
    repo: str,
    pr: int,
    threads: tuple[Thread, ...],
    verdict: Verdict,
    branche: str | None,
    url_pr: str | None,
    derivee: bool,
) -> tuple[int, int, int]:
    """Repond dans chaque fil, et resout ceux qui le meritent.

    Rend `(repondus, resolus, demandes)`. Un echec sur UN fil ne prive pas les
    autres de leur reponse : les fils sont independants, et l'agent qui aurait
    tout corrige mais rien publie ressemble a un agent qui n'a rien fait.
    """
    if writer is None:
        return (0, 0, 0)

    repondus = resolus = demandes = 0
    for t in threads:
        v = verdict.for_thread(t.id)
        if v is None:
            # Inatteignable par le chemin normal : `verdict.parse` ajoute deja
            # en ARBITRAGE tout fil soumis que l'agent n'evoque pas. On ne passe
            # donc ici que si `threads` et `submitted` ont diverge — un defaut
            # de programmation, pas un cas metier.
            journal.emit(Event(
                event="job.thread_hors_verdict", profile=profile.project,
                repository=repo, pull_request=pr,
                why=f"fil {t.id[:12]}… absent du verdict ET de `submitted`",
            ))
            continue
        corps = report.thread_reply(
            v, notify=profile.human.notify, branch=branche,
            pull_url=url_pr, derived=derivee,
        )
        try:
            await writer.reply_in_thread(
                t.id, corps, awaiting_human=v.outcome.needs_human)
            repondus += 1
            if v.outcome.needs_human:
                demandes += 1
            if v.outcome.resolves:
                # Le fil resolu est ce qui compte comme solde, pas la reponse :
                # un correctif pousse, argumente et vert dont le fil reste
                # ouvert laisse la remarque comptee comme ouverte.
                await writer.resolve_thread(t.id)
                resolus += 1
        except ForgeError as e:
            journal.emit(Event(
                event="job.reply_failed", profile=profile.project,
                repository=repo, pull_request=pr,
                why=f"fil {t.id[:12]}… : {e}",
            ))
    return (repondus, resolus, demandes)


async def prevenir_humain(
    *,
    writer,
    journal: Journal,
    store: StateStore,
    profile: ProfileConfig,
    repo: str,
    pr: int,
    snap: PullSnapshot,
    decision: Decision,
    etat: PullState,
) -> int:
    """Previent l'humain SANS lancer d'agent : cycles epuises, CI bloquee.

    Rend le nombre d'appels reellement poses — zero veut dire « deja signale,
    rien de nouveau a dire », pas « echec ».

    Deux voies, et le choix se fait sur la MATIERE : s'il y a des fils, la
    question va dans les fils, la ou la remarque a ete ecrite. Sinon — une CI
    rouge n'appartient a aucun fil — elle va sur la PR.
    """
    from reviewer.rules.machine import Action  # noqa: PLC0415 — cycle

    if writer is None:
        return 0

    # Fils sur lesquels l'agent n'a pas encore pose de question. Ceux qui
    # attendent deja portent leur marqueur : les relancer reposerait la meme
    # question a chaque passage.
    a_poser = [t for t in decision.threads if not t.awaiting_human]
    pose = 0
    avis = report.blocked_notice(
        decision.reason, repo=repo, pr=pr, notify=profile.human.notify,
        review_cycle=etat.review_cycle,
        max_review_cycles=profile.max_review_cycles,
    )
    try:
        for t in a_poser:
            await writer.reply_in_thread(t.id, avis, awaiting_human=True)
            pose += 1

        if not a_poser and etat.human_asked_sha != snap.head_sha:
            # Aucun fil ou poser la question, et on n'a pas encore appele pour
            # CET etat : commentaire de PR, une seule fois par tete.
            await writer.comment_on_pull(repo, pr, avis, awaiting_human=True)
            pose += 1
            store.save_pull_state(replace(etat, human_asked_sha=snap.head_sha))

        if (profile.human.label_needs_human
                and Action.LABEL_NEEDS_HUMAN in decision.actions):
            await writer.add_label(repo, pr, profile.human.label_needs_human)
    except ForgeError as e:
        journal.emit(Event(
            event="job.notify_failed", profile=profile.project,
            repository=repo, pull_request=pr, why=str(e),
        ))
        return pose

    if pose:
        journal.emit(Event(
            event="job.asked_human", profile=profile.project,
            repository=repo, pull_request=pr, state=decision.state.value,
            result="asked", why=f"{pose} appel(s) a l'humain : {decision.reason}",
        ))
    return pose


async def signaler_arret(
    *,
    writer,
    journal: Journal,
    store: StateStore,
    profile: ProfileConfig,
    repo: str,
    pr: int,
    snap: PullSnapshot,
    etat: PullState,
    raison: str,
    branche: str | None,
    checks=None,
    anomalies: tuple[str, ...] = (),
    fils: tuple[Thread, ...] = (),
) -> bool:
    """Dit qu'on s'arrete, et a qui de derouler. Rend `True` si quelqu'un a ete prevenu.

    LES FILS D'ABORD. Un arret qui ne publiait qu'un commentaire general
    laissait les fils de revue muets : la personne qui a ecrit la remarque ne
    voyait rien dans SON fil et devait deviner qu'un commentaire ailleurs la
    concernait. Chaque fil soumis recoit donc un mot, meme quand rien n'a abouti.

    Aucun n'est RESOLU : on ne solde pas ce qu'on n'a pas traite.

    Le tout sous le MEME garde que le commentaire (`human_asked_sha`) : sans lui,
    chaque cycle rate republierait dans les memes fils, et trois cycles feraient
    trois fois le meme message a la meme personne.
    """
    if writer is None or etat.human_asked_sha == snap.head_sha:
        return False

    for t in fils:
        try:
            await writer.reply_in_thread(
                t.id,
                report.thread_unanswered(
                    raison, notify=profile.human.notify, branch=branche),
                awaiting_human=True,
            )
        except ForgeError as e:
            journal.emit(Event(
                event="job.reply_failed", profile=profile.project,
                repository=repo, pull_request=pr,
                why=f"fil {t.id[:12]}… (arret) : {e}",
            ))

    try:
        await writer.comment_on_pull(
            repo, pr,
            report.blocked_notice(
                raison, repo=repo, pr=pr, notify=profile.human.notify,
                review_cycle=etat.review_cycle + 1,
                max_review_cycles=profile.max_review_cycles,
                anomalies=anomalies,
                # Ce que la commande a DIT : la seule chose qui permette de
                # trancher entre « le correctif est mauvais » et
                # « l'environnement est incomplet ».
                extrait=checks.failure.tail() if (
                    checks is not None and checks.failure) else "",
            ),
            awaiting_human=True,
        )
    except ForgeError as e:
        journal.emit(Event(
            event="job.notify_failed", profile=profile.project,
            repository=repo, pull_request=pr, why=str(e),
        ))
        return False

    store.save_pull_state(replace(
        store.pull_state(profile.project, repo, pr),
        human_asked_sha=snap.head_sha))
    return True
