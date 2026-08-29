"""API locale — l'unique dependance du front de visualisation.

Elle existe DES LE LOT 1, alors que le front est un chantier ulterieur. Ce n'est
pas de l'anticipation gratuite : un front qui doit reparser des lignes de
journal reconstruit un etat approximatif, et il sera faux. Poser la frontiere
maintenant coute cent lignes ; la poser apres coup coute de rendre le journal
retro-compatible, ce qu'il n'est jamais.

DEUX SURFACES, ET ELLES NE SERVENT PAS A LA MEME CHOSE :

    GET /jobs     l'ETAT — ce qui est vrai maintenant. Idempotent, rejouable,
                  c'est ce qu'on affiche au chargement d'une page.
    GET /events   le FLUX — ce qui vient de changer (SSE). C'est ce qui evite
                  au front de sonder.

Un front qui n'aurait que le flux raterait tout ce qui s'est passe avant sa
connexion ; un front qui n'aurait que l'etat devrait sonder. Les deux.

ELLE N'ECOUTE QUE SUR LA BOUCLE LOCALE, et la validation de configuration
refuse `0.0.0.0`. Le demon n'expose aucun port entrant : les evenements
arrivent par une connexion SORTANTE.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from agent_runner_lg.config import Access, ProfileConfig, RunnerConfig
from agent_runner_lg.output.console import PAGE
from agent_runner_lg.output.events import Journal, relire
from agent_runner_lg.rules.machine import Severity
from agent_runner_lg.store.leases import StateStore

__all__ = ["create_app"]


def _lease_json(lease) -> dict[str, Any]:
    d = asdict(lease)
    d["acquired_at"] = lease.acquired_at.isoformat()
    d["expires_at"] = lease.expires_at.isoformat()
    # Les DUREES sont calculees ici, pas dans le navigateur. Une horloge de
    # client desynchronisee — ou simplement dans un autre fuseau — afficherait
    # « ce job tourne depuis -2 h », et on chercherait la panne du mauvais cote.
    # Le serveur est la seule horloge qui fasse autorite sur ses propres baux.
    maintenant = datetime.now(timezone.utc)
    d["age_s"] = (maintenant - lease.acquired_at).total_seconds()
    d["expires_in_s"] = (lease.expires_at - maintenant).total_seconds()
    # `repository` / `pull_request` : memes noms que dans le journal. Deux
    # vocabulaires pour une meme chose obligent le front a savoir lequel vient
    # d'ou, et c'est exactement ce qu'une frontiere d'API doit epargner.
    d["repository"] = lease.repo
    d["pull_request"] = lease.pr
    return d


def create_app(runner: RunnerConfig, profils: dict[str, ProfileConfig],
               store: StateStore, journal: Journal,
               *, sse_keepalive_s: float = 20.0) -> FastAPI:
    app = FastAPI(title="claude-agent-runner", version="0.1.0",
                  description="Etat et flux d'evenements du demon local.")

    @app.get("/health")
    def health() -> dict[str, Any]:
        # `writes_enabled` est expose ICI parce que c'est la premiere question
        # qu'on se pose devant un comportement inattendu : est-ce que ce demon
        # a le droit d'ecrire ?
        return {
            "ok": True,
            "writes_enabled": runner.writes_enabled,
            "max_parallel": runner.max_parallel,
            "profiles": sorted(profils),
        }

    @app.get("/", include_in_schema=False)
    def console() -> HTMLResponse:
        """La console. Servie par le demon lui-meme, sans fichier a deployer."""
        return HTMLResponse(PAGE)

    @app.get("/graph")
    def graphe() -> dict[str, Any]:
        """Le graphe tel qu'il est CABLE : de quoi le dessiner sans le deviner.

        Servi plutot que recopie dans la page. Un schema fige dans le front
        derive du cablage a la premiere modification, et rien ne le signale —
        un dessin faux ne leve aucune erreur.
        """
        from agent_runner_lg.graph.build import topologie  # noqa: PLC0415

        return topologie()

    @app.get("/history")
    def history(jours: int = 3, limite: int = 120) -> dict[str, Any]:
        """Les cycles passes, reconstitues depuis les fichiers de journal.

        ── POURQUOI LE SERVEUR, ET PAS LE NAVIGATEUR ───────────────────────

        Un historique garde cote navigateur disparait au premier vidage de
        cache, ne suit pas d'un poste a l'autre, et ne dit rien de ce qui s'est
        passe pendant que la page etait fermee — c'est-a-dire l'essentiel, pour
        un demon qui tourne la nuit.

        Le journal sur disque, lui, est la meme verite que celle que lit la
        ligne de commande. Une seule source.

        Le STATUT n'est pas stocke : il se DEDUIT des evenements. Un statut
        ecrit quelque part serait une seconde verite, qui finirait par
        contredire le journal — et le jour ou elle le contredirait, personne ne
        saurait laquelle croire.
        """
        actifs = {b.job_id for b in store.active_leases()}
        par_job: dict[str, list] = {}
        for e in relire(runner.logs_dir, jours=jours):
            if e.job_id:
                par_job.setdefault(e.job_id, []).append(e)

        jobs = []
        for job_id, evs in par_job.items():
            chemin: list[str] = []
            for e in evs:
                if e.event == "graph.node" and e.state and (
                        not chemin or chemin[-1] != e.state):
                    chemin.append(e.state)
            fins = {e.event: e for e in evs
                    if e.event in ("job.finished", "job.needs_human", "job.dry_run")}
            tete = evs[0]
            if "job.needs_human" in fins:
                statut, fin = "echec", fins["job.needs_human"]
            elif "job.dry_run" in fins:
                statut, fin = "a_blanc", fins["job.dry_run"]
            elif "job.finished" in fins:
                fin = fins["job.finished"]
                statut = "attente" if fin.state == "NEEDS_HUMAN" else "termine"
            elif job_id in actifs:
                statut, fin = "en_cours", evs[-1]
            else:
                # Ni fin, ni bail : le processus est mort en route. Le DIRE —
                # un cycle interrompu qu'on affiche « en cours » laisse croire
                # a un travail qui n'avance pas, et on cherche la panne ailleurs.
                statut, fin = "interrompu", evs[-1]

            jobs.append({
                "job_id": job_id,
                "repository": tete.repository,
                "pull_request": tete.pull_request,
                "profile": tete.profile,
                "statut": statut,
                "debut": tete.ts,
                "fin": fin.ts,
                "chemin": chemin,
                "raison": fin.why or "",
                "etat": fin.state,
                "cout": (fin.detail or {}).get("cost_usd"),
                "evenements": len(evs),
            })

        jobs.sort(key=lambda j: j["debut"], reverse=True)
        return {"jobs": jobs[:limite]}

    @app.get("/history/{job_id}")
    def history_detail(job_id: str, jours: int = 3) -> dict[str, Any]:
        """Le fil complet d'un cycle, etapes d'agent comprises."""
        evs = [asdict(e) for e in relire(runner.logs_dir, jours=jours)
               if e.job_id == job_id]
        if not evs:
            raise HTTPException(404, f"aucun evenement pour {job_id}")
        return {"job_id": job_id, "events": evs}

    @app.get("/profiles")
    def profiles() -> list[dict[str, Any]]:
        return [
            {
                "project": p.project,
                "org": p.forge.org,
                "max_review_cycles": p.max_review_cycles,
                "repos": {
                    nom: {"access": r.access.value, "checks": len(r.checks)}
                    for nom, r in p.repos.items()
                },
                # Compte des depots ECRIVABLES, mis en avant : c'est le seul
                # chiffre qui dit ce que ce profil autorise reellement.
                "writable": sorted(p.repos_by_access(Access.WRITE)),
                # Le moteur RESOLU, severite par severite — pas la table brute
                # du YAML. Une entree qui ne nomme que l'effort retombe sur le
                # modele global, et afficher le fichier tel quel laisserait
                # croire qu'aucun modele ne s'applique.
                "engine": {
                    s.name: dict(zip(("model", "effort"), p.moteur(s)))
                    for s in Severity
                },
            }
            for p in profils.values()
        ]

    @app.get("/jobs")
    def jobs() -> dict[str, Any]:
        """Ce qui tourne, et ce qui vient de se passer.

        Les baux sont l'etat REEL — pas une projection. Un job qui n'a pas de
        bail ne tourne pas, quelle que soit la derniere ligne du journal.
        """
        # DEUX familles d'evenements sont ECARTEES d'ici, pour la meme raison :
        # elles noieraient les transitions qui portent une decision.
        #
        #   agent.step   60 a 120 par job mesure — que des `Read` et des `Edit`
        #   graph.node   12 par cycle — le deplacement dans le graphe, qui a sa
        #                propre representation dans la console
        #
        # Les deux restent dans le FLUX (`/events`) et dans le detail d'un job :
        # c'est la vue generale qu'elles rendraient illisible, pas le suivi.
        return {
            "active": [_lease_json(b) for b in store.active_leases()],
            "recent": [asdict(e) for e in journal.recent(400)
                       if e.event not in ("agent.step", "graph.node")][-50:],
        }

    @app.get("/jobs/{profile}/{repo}/{pr}")
    def job_detail(profile: str, repo: str, pr: int) -> dict[str, Any]:
        if profile not in profils:
            raise HTTPException(404, f"profil inconnu : {profile}")
        lease = store.lease(profile, repo, pr)
        return {
            "profile": profile,
            "repository": repo,
            "pull_request": pr,
            "lease": _lease_json(lease) if lease else None,
            "state": asdict(store.pull_state(profile, repo, pr)),
            "events": [
                asdict(e) for e in journal.recent(200)
                if e.profile == profile and e.repository == repo and e.pull_request == pr
            ],
        }

    @app.get("/events")
    async def events() -> StreamingResponse:
        """Flux SSE. Un commentaire de battement evite les coupures d'inactivite.

        L'intervalle est un PARAMETRE : il borne aussi le temps qu'un client qui
        se deconnecte peut laisser la coroutine en attente. Une valeur figee a
        20 s rendait la suite de tests vingt fois plus lente qu'elle n'a besoin
        de l'etre — et un reglage qu'on ne peut pas baisser en test est un
        reglage qu'on ne teste pas.
        """

        async def flux() -> AsyncIterator[bytes]:
            abonnement = journal.subscribe()
            # On rejoue les derniers evenements a la connexion : sans ca, un
            # front qui se connecte affiche un ecran vide jusqu'a la prochaine
            # transition — qui peut ne jamais venir.
            for e in journal.recent(20):
                yield f"data: {json.dumps(asdict(e), ensure_ascii=False)}\n\n".encode()
            while True:
                try:
                    e = await asyncio.wait_for(anext(abonnement),
                                               timeout=sse_keepalive_s)
                except asyncio.TimeoutError:
                    # Les proxys et les navigateurs ferment une connexion
                    # silencieuse. Un commentaire SSE la tient ouverte sans
                    # polluer le flux de donnees.
                    yield b": keep-alive\n\n"
                    continue
                except (StopAsyncIteration, asyncio.CancelledError):
                    break
                yield f"data: {json.dumps(asdict(e), ensure_ascii=False)}\n\n".encode()

        return StreamingResponse(flux(), media_type="text/event-stream", headers={
            "cache-control": "no-cache",
            # Sans ca, un proxy tamponne le flux et le front ne recoit rien
            # avant la fermeture — le symptome ressemble a « le demon n'emet
            # pas d'evenements ».
            "x-accel-buffering": "no",
        })

    return app
