"""La console locale : une page, servie par l'API, sans rien a construire.

── CE QU'ELLE MONTRE ───────────────────────────────────────────────────────

Le graphe, et l'endroit ou en est le cycle SELECTIONNE. Tout le reste sert a
choisir ce cycle : la liste de gauche porte l'historique, chacun avec son depot,
sa PR et son statut.

C'est la seule representation qui reponde a la question qu'on se pose devant un
demon qui tourne : « il en est ou, la ? » Une liste d'evenements n'y repond pas.
Elle dit ce qui vient d'arriver, pas ce qui est en train de se passer — et sur
un noeud qui dure trente minutes (`code`), la derniere ligne du journal a une
demi-heure et ressemble a un blocage.

── OU VIT QUOI ─────────────────────────────────────────────────────────────

Le DESSIN n'est pas ecrit ici : les noeuds et les arcs viennent de `GET /graph`,
qui lit le graphe compile. Un schema recopie dans le front derive du cablage a
la premiere modification, et rien ne le signale — un dessin faux ne leve aucune
erreur, il se contente d'etre faux.

L'HISTORIQUE non plus : il vient de `GET /history`, reconstitue depuis les
fichiers de journal. Un historique garde cote navigateur disparait au premier
vidage de cache et ne dit rien de ce qui s'est passe pendant que la page etait
fermee — c'est-a-dire l'essentiel, pour un demon qui tourne la nuit.

Ce qui vit ici, et rien d'autre : la MISE EN PAGE — ou tombe chaque noeud, dans
les deux orientations — et l'apparence. Ce sont des decisions visuelles.

── CE QU'ELLE NE FAIT PAS, ET POURQUOI ─────────────────────────────────────

Elle ne modifie AUCUN reglage, et n'efface AUCUN journal. Le YAML est la
frontiere de securite du demon ; les fichiers de journal sont sa memoire.
« Vider » masque donc la vue, il ne supprime rien sur le disque — la borne est
retenue localement. Une console capable d'effacer des traces serait une console
capable de faire disparaitre la preuve d'un incident.

Le bouton « Test » simule un cycle ENTIEREMENT dans le navigateur. Il n'appelle
rien et n'ecrit rien ; le cycle porte le statut « test » pour qu'on ne le
confonde jamais avec un vrai — une demonstration qui ressemble a la production
finit par tromper quelqu'un.

── AUCUNE DEPENDANCE ───────────────────────────────────────────────────────

Pas de CDN, pas de police distante, pas d'etape de construction. Le demon tourne
sur une machine de developpement, parfois hors ligne, et une console qui depend
du reseau est une console qui manque le jour ou l'on debogue une panne reseau.
"""

from __future__ import annotations

__all__ = ["PAGE"]

PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent runner</title>
<style>
  /* Sombre, assume. Pas de variante claire : cette console se regarde a cote
     d'un terminal, et une page qui bascule au blanc selon le reglage du systeme
     aveugle a chaque fois. */
  :root {
    color-scheme: dark;
    --vide:    #04070A;
    --panneau: #0A1218;
    --trait:   #16262E;
    --trait2:  #213742;
    --encre:   #E2F5F0;
    --doux:    #8AA5AE;
    --pale:    #56717C;

    --neon:    #2BE8B0;
    --cyan:    #45D2FF;
    --violet:  #9D7BFF;
    --ambre:   #FFC061;
    --rouge:   #FF6E7E;

    --lueur:   0 0 22px rgba(43,232,176,.40);
    --sans: "Segoe UI Variable Text", "Segoe UI", Inter, system-ui, -apple-system, sans-serif;
    --mono: "Cascadia Mono", "JetBrains Mono", ui-monospace, Consolas, monospace;
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--vide); color: var(--encre);
    font: 14px/1.55 var(--sans); -webkit-font-smoothing: antialiased;
    display: flex; flex-direction: column; overflow: hidden;
  }
  /* Deux taches tres diluees : de la profondeur, sans coloriser quoi que ce soit. */
  body::before {
    content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background:
      radial-gradient(60vw 42vh at 16% 0%, rgba(43,232,176,.06), transparent 70%),
      radial-gradient(52vw 42vh at 94% 10%, rgba(69,210,255,.05), transparent 70%);
  }
  #app { position: relative; z-index: 1; display: flex; flex-direction: column; height: 100%; }

  /* ── Barre du haut ───────────────────────────────────────────────────── */
  header {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 11px 18px; border-bottom: 1px solid var(--trait);
    background: linear-gradient(180deg, rgba(14,26,33,.92), rgba(10,18,24,.72));
    backdrop-filter: blur(8px); flex: 0 0 auto;
  }
  .marque { display: flex; align-items: center; gap: 9px; }
  .pouls {
    width: 7px; height: 7px; border-radius: 50%; background: var(--trait2);
    transition: background .3s, box-shadow .3s;
  }
  .pouls.vif { background: var(--neon); box-shadow: 0 0 0 4px rgba(43,232,176,.16), var(--lueur); }
  h1 {
    font: 640 14px/1 var(--sans); margin: 0; letter-spacing: .06em;
    background: linear-gradient(92deg, var(--neon), var(--cyan));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .sous { color: var(--pale); font-size: 12px; }

  .outils { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .jauge {
    font: 11.5px/1 var(--mono); padding: 6px 10px; border-radius: 8px;
    border: 1px solid var(--trait); color: var(--doux); background: rgba(14,26,33,.6);
    white-space: nowrap;
  }
  .jauge b { color: var(--encre); }
  .jauge.arme { border-color: var(--ambre); color: var(--ambre); }

  button {
    font: 640 11px/1 var(--sans); letter-spacing: .07em; text-transform: uppercase;
    padding: 8px 12px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--trait2); background: rgba(14,26,33,.7); color: var(--doux);
    transition: border-color .18s, color .18s, background .18s, box-shadow .18s;
  }
  button:hover { border-color: var(--neon); color: var(--neon); box-shadow: 0 0 14px rgba(43,232,176,.16); }
  button.vif { border-color: var(--neon); color: var(--neon); background: rgba(43,232,176,.10); }
  .bascule { display: flex; border: 1px solid var(--trait2); border-radius: 8px; overflow: hidden; }
  .bascule button { border: 0; border-radius: 0; background: transparent; }
  .bascule button + button { border-left: 1px solid var(--trait2); }
  .bascule button[aria-pressed="true"] { background: rgba(43,232,176,.12); color: var(--neon); }

  /* ── Corps ───────────────────────────────────────────────────────────── */
  main {
    flex: 1 1 auto; min-height: 0;
    display: grid; grid-template-columns: 296px minmax(0, 1fr); gap: 14px;
    padding: 14px 18px 18px;
  }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .colonne { display: flex; flex-direction: column; gap: 14px; min-height: 0; min-width: 0; }

  /* La rangee du bas : ce que le demon FAIT, et ce sur quoi il le fait. Lire
     le fil sans la PR obligeait a ouvrir GitHub pour savoir de quelle remarque
     on parlait — donc a quitter la console pour la comprendre. */
  .rangee { display: flex; gap: 14px; min-height: 0; min-width: 0; }
  .rangee > .panneau { flex: 1 1 50%; min-width: 0; }
  @media (max-width: 1280px) { .rangee { flex-direction: column; } }

  .pr { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 10px 12px;
        display: flex; flex-direction: column; gap: 9px; }
  .pr .ligne { display: flex; gap: 9px; align-items: baseline; }
  .pr .cle { flex: 0 0 76px; color: var(--pale); font: 10px var(--mono);
             text-transform: uppercase; letter-spacing: .11em; }
  .pr .val { font: 12px var(--mono); color: var(--encre); word-break: break-word; }
  .pr .bloc { font: 640 10px var(--sans); text-transform: uppercase; margin-top: 5px;
              letter-spacing: .14em; color: var(--pale); }
  .pr .item { border: 1px solid var(--trait); border-radius: 10px; padding: 8px 10px;
              display: flex; flex-direction: column; gap: 5px; }
  .pr .item.attente { border-color: var(--ambre); }
  .pr .item.resolu { opacity: .48; }
  .pr .ou { font: 10.5px var(--mono); color: var(--cyan); word-break: break-all; }
  .pr .msg { font-size: 12px; color: var(--doux); white-space: pre-wrap; word-break: break-word; }
  .pr .qui { color: var(--pale); font: 10px var(--mono); }
  .pr .chk { display: flex; gap: 7px; align-items: center; font: 11px var(--mono); color: var(--doux); }
  .pr .puce2 { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; background: var(--pale); }
  .pr .puce2.ok { background: var(--neon); }
  .pr .puce2.ko { background: var(--rouge); }
  .pr .lien { color: var(--cyan); text-decoration: none; border-bottom: 1px solid transparent; }
  .pr .lien:hover { border-bottom-color: var(--cyan); }

  /* La carte d'une PR n'est plus un <button> : elle CONTIENT un lien vers la
     forge et un bouton de reprise, et un bouton dans un bouton n'existe pas. */
  .cycle .lien { color: var(--encre); text-decoration: none; border-bottom: 1px solid transparent; }
  .cycle .lien:hover { color: var(--cyan); border-bottom-color: var(--cyan); }
  .cycle .reprendre { margin-left: auto; padding: 3px 8px; font-size: 9.5px; }

  .panneau {
    background: linear-gradient(180deg, rgba(14,26,33,.72), rgba(10,18,24,.88));
    border: 1px solid var(--trait); border-radius: 14px;
    display: flex; flex-direction: column; min-height: 0; overflow: hidden;
    box-shadow: 0 22px 54px -32px rgba(0,0,0,.95);
  }
  .entete {
    display: flex; align-items: center; gap: 10px; flex: 0 0 auto;
    padding: 11px 14px; border-bottom: 1px solid var(--trait);
  }
  h2 {
    font: 640 10.5px/1 var(--sans); margin: 0; color: var(--pale);
    text-transform: uppercase; letter-spacing: .14em;
  }
  .droite { margin-left: auto; display: flex; gap: 8px; align-items: center; min-width: 0; }
  .cible { font: 12.5px var(--mono); color: var(--neon); }

  /* ── Liste des cycles ────────────────────────────────────────────────── */
  .cycles { overflow-y: auto; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
  .cycle {
    text-align: left; text-transform: none; letter-spacing: 0; font-weight: 500;
    padding: 9px 11px; border-radius: 10px; border: 1px solid var(--trait);
    background: rgba(8,14,19,.55); display: grid; gap: 4px; width: 100%;
  }
  .cycle:hover { border-color: var(--trait2); color: inherit; box-shadow: none; }
  .cycle[aria-selected="true"] {
    border-color: var(--neon); background: rgba(43,232,176,.08);
    box-shadow: inset 0 0 0 1px rgba(43,232,176,.16);
  }
  .cycle .haut, .cycle .bas { display: flex; align-items: center; gap: 7px; }
  .cycle .depot { font: 600 13px var(--mono); color: var(--encre); }
  .cycle .quand, .cycle .etapes { margin-left: auto; font: 10.5px var(--mono); color: var(--pale); }
  /* Le nombre de passages d'un groupe. Colle au nom du depot — il le qualifie,
     il n'est pas une donnee de plus alignee a droite. */
  .cycle .fois {
    font: 600 9.5px var(--mono); color: var(--cyan);
    border: 1px solid var(--trait2); border-radius: 5px; padding: 1px 4px;
  }
  .statut {
    font: 640 9.5px/1 var(--sans); text-transform: uppercase; letter-spacing: .1em;
    padding: 3px 7px; border-radius: 999px; border: 1px solid currentColor;
  }
  .s-en_cours   { color: var(--neon); }
  .s-termine    { color: var(--cyan); }
  .s-attente    { color: var(--ambre); }
  .s-echec      { color: var(--rouge); }
  .s-a_blanc    { color: var(--violet); }
  .s-interrompu { color: var(--pale); }
  .s-test       { color: var(--violet); }
  /* Les etats d'une PR. Ils ne disent pas la meme chose qu'un statut de cycle :
     ils disent ce qu'on ATTEND, et de qui. */
  .s-NEEDS_FIX       { color: var(--neon); }
  .s-AGENT_WORKING   { color: var(--neon); }
  .s-WAITING_CI      { color: var(--cyan); }
  .s-WAITING_REVIEW  { color: var(--cyan); }
  .s-NEEDS_HUMAN     { color: var(--ambre); }
  .s-READY_FOR_HUMAN { color: var(--violet); }
  .s-IDLE            { color: var(--pale); }
  .pourquoi {
    grid-column: 1 / -1; color: var(--pale); font-size: 11.5px; line-height: 1.45;
    margin-top: 2px;
  }
  .cycle.vif .statut { animation: battement 1.7s ease-in-out infinite; }
  @keyframes battement { 0%,100% { opacity: 1; } 50% { opacity: .4; } }

  /* ── Le graphe ───────────────────────────────────────────────────────── */
  /* La toile est un PLAN, pas une image : elle occupe tout le panneau et c'est
     la camera (le groupe `#cam`) qui cadre. Laisser le SVG se dimensionner sur
     son `viewBox` donnait un graphe horizontal haut de 240 px dans un panneau
     de 750 — un quart de la surface, et rien a faire des trois autres quarts. */
  .toile {
    flex: 1 1 auto; min-height: 320px; position: relative; overflow: hidden;
    touch-action: none;
  }
  svg {
    display: block; width: 100%; height: 100%; cursor: grab;
    /* Sans ca, glisser pour deplacer SELECTIONNE les libelles des noeuds : le
       navigateur ne voit qu'un glisser sur du texte. Cible sur le SVG et pas
       sur la page, pour qu'on puisse toujours copier une ligne du fil. */
    user-select: none; -webkit-user-select: none;
  }
  svg.tire { cursor: grabbing; }

  .navig {
    position: absolute; right: 12px; bottom: 12px; display: flex; gap: 6px;
    background: rgba(6,12,17,.82); border: 1px solid var(--trait);
    border-radius: 10px; padding: 5px; backdrop-filter: blur(6px);
  }
  .navig button { padding: 6px 9px; min-width: 32px; letter-spacing: 0; font-size: 12px; }
  .echelle {
    position: absolute; left: 12px; bottom: 12px; padding: 5px 9px;
    font: 11px var(--mono); color: var(--pale);
    background: rgba(6,12,17,.82); border: 1px solid var(--trait); border-radius: 8px;
  }

  .arc { fill: none; stroke: var(--trait2); color: var(--trait2); stroke-width: 1.4; }
  .arc.conditionnel { stroke-dasharray: 3 5; }
  /* En condense, les arrets et le contournement se croisent : traces faibles,
     ils rappellent que le chemin existe sans encombrer celui qu'on suit. */
  .arc.libre { opacity: .38; }
  .arc.libre.pris, .arc.libre.encours { opacity: 1; }
  .arc.pris {
    stroke: var(--neon); color: var(--neon); stroke-width: 2.2; stroke-dasharray: none;
    filter: drop-shadow(0 0 5px rgba(43,232,176,.55));
  }
  .arc.encours {
    stroke: var(--neon); color: var(--neon); stroke-width: 2.2; stroke-dasharray: 6 6;
    filter: drop-shadow(0 0 8px rgba(43,232,176,.7)); animation: file .9s linear infinite;
  }
  @keyframes file { to { stroke-dashoffset: -12; } }

  .noeud rect.corps {
    fill: rgba(10,20,26,.92); stroke: var(--trait2); stroke-width: 1.4;
    transition: fill .3s, stroke .3s;
  }
  .noeud .titre { font: 640 12px var(--sans); fill: var(--pale); transition: fill .3s; }
  .noeud .note  { font: 10px var(--mono); fill: var(--trait2); transition: fill .3s; }

  .noeud.fait rect.corps { fill: rgba(43,232,176,.10); stroke: rgba(43,232,176,.55); }
  .noeud.fait .titre { fill: var(--encre); }
  .noeud.fait .note  { fill: var(--doux); }

  .noeud.actif rect.corps {
    fill: var(--neon); stroke: var(--neon);
    filter: drop-shadow(0 0 16px rgba(43,232,176,.75));
  }
  .noeud.actif .titre { fill: #052018; }
  .noeud.actif .note  { fill: rgba(5,32,24,.72); }
  .noeud.actif .halo { opacity: .6; animation: souffle 1.9s ease-in-out infinite; }

  .noeud.arret rect.corps { fill: rgba(255,110,126,.12); stroke: var(--rouge); stroke-width: 2; }
  .noeud.arret .titre { fill: var(--rouge); }
  .noeud.arret .note  { fill: rgba(255,110,126,.7); }
  .noeud.fin rect.corps { stroke-width: 2.2; }

  .halo { fill: none; stroke: var(--neon); stroke-width: 1.5; opacity: 0;
          transform-origin: center; transform-box: fill-box; }
  @keyframes souffle {
    0%,100% { transform: scale(1);    opacity: .6; }
    50%     { transform: scale(1.09); opacity: 0; }
  }

  .legende {
    flex: 0 0 auto; display: flex; gap: 16px; flex-wrap: wrap;
    padding: 9px 14px; border-top: 1px solid var(--trait);
    font: 11px var(--sans); color: var(--pale);
  }
  .legende span { display: flex; align-items: center; gap: 6px; }
  .puce { width: 9px; height: 9px; border-radius: 3px; border: 1.5px solid var(--trait2); }
  .puce.fait  { background: rgba(43,232,176,.16); border-color: rgba(43,232,176,.6); }
  .puce.actif { background: var(--neon); border-color: var(--neon); box-shadow: 0 0 8px rgba(43,232,176,.7); }
  .puce.arret { border-color: var(--rouge); background: rgba(255,110,126,.15); }

  /* ── Fil ─────────────────────────────────────────────────────────────── */
  .fil { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
  .ligne {
    display: grid; grid-template-columns: 58px 76px 1fr; gap: 10px; align-items: baseline;
    padding: 6px 14px; border-bottom: 1px solid rgba(22,38,46,.55); font-size: 13px;
  }
  .ligne:last-child { border-bottom: 0; }
  .ligne .h { color: var(--pale); font: 11px var(--mono); }
  .ligne .quoi { font: 640 10px var(--sans); text-transform: uppercase; letter-spacing: .09em; color: var(--pale); }
  .ligne .txt { color: var(--doux); overflow-wrap: anywhere; }
  .ligne.etape .txt { font: 12px var(--mono); color: var(--encre); }
  .ligne.noeud-fil .quoi { color: var(--neon); }
  .ligne.noeud-fil .txt { color: var(--encre); font-weight: 560; }
  .ligne.mauvais .quoi, .ligne.mauvais .txt { color: var(--rouge); }
  .ligne.bon .quoi { color: var(--cyan); }
  /* Ce que l'agent DIT : en prose et non en monospace — c'est du raisonnement,
     pas une commande. */
  .ligne.dit .quoi { color: var(--violet); }
  .ligne.dit .txt { color: var(--doux); font-style: italic; }

  .ligne.pliable { cursor: pointer; }
  .ligne.pliable .quoi::after { content: " +"; color: var(--trait2); }
  .ligne.pliable.ouvert .quoi::after { content: " -"; }
  .ligne .detail { display: none; }
  .ligne.ouvert .detail {
    display: block; grid-column: 2 / -1; margin: 6px 0 2px;
    padding: 9px 11px; border-radius: 8px; background: rgba(4,7,10,.7);
    border: 1px solid var(--trait); color: var(--doux);
    font: 11.5px/1.55 var(--mono); white-space: pre-wrap; overflow-x: auto;
    max-height: 260px; overflow-y: auto;
  }
  .vide { padding: 46px 16px; text-align: center; color: var(--pale); font-size: 13px; }

  /* ── Modale ──────────────────────────────────────────────────────────── */
  dialog {
    border: 1px solid var(--trait2); border-radius: 14px; padding: 0;
    background: var(--panneau); color: var(--encre);
    width: min(960px, 92vw); max-height: 82vh;
    display: none; flex-direction: column;
  }
  dialog[open] { display: flex; }
  dialog::backdrop { background: rgba(2,5,8,.74); backdrop-filter: blur(3px); }

  ::-webkit-scrollbar { width: 9px; height: 9px; }
  ::-webkit-scrollbar-thumb { background: var(--trait2); border-radius: 9px; }
  ::-webkit-scrollbar-track { background: transparent; }
</style>
</head>
<body>
<div id="app">

<header>
  <div class="marque">
    <span class="pouls" id="pouls"></span>
    <h1>AGENT RUNNER</h1>
    <span class="sous">console locale</span>
  </div>
  <div class="outils">
    <span class="jauge" id="armement">&hellip;</span>
    <span class="jauge"><b id="actifs">0</b> en cours</span>
    <span class="jauge"><b id="parallele">&ndash;</b> de front</span>
    <div class="bascule">
      <button id="b-cond" aria-pressed="true" title="Disposition calculee pour la place disponible">&#9638; Condense</button>
      <button id="b-vert" aria-pressed="false" title="Graphe vertical">&#8597; Vertical</button>
      <button id="b-hori" aria-pressed="false" title="Graphe horizontal">&#8596; Horizontal</button>
    </div>
    <button id="b-balayer" title="Relire la forge maintenant, sans attendre l'intervalle">Relancer</button>
    <button id="b-test" title="Simule un cycle dans le navigateur, sans toucher au demon">Test</button>
    <button id="b-journal">Journal</button>
    <button id="b-vider" title="Masque les cycles passes ; n'efface rien sur le disque">Vider</button>
  </div>
</header>

<main>
  <div class="colonne">
    <section class="panneau" style="flex: 0 1 auto; max-height: 52%;">
      <div class="entete">
        <h2>PR suivies</h2>
        <div class="droite"><span class="jauge" id="balaye">&mdash;</span></div>
      </div>
      <div class="cycles" id="pulls"><div class="vide">Pas encore balaye.</div></div>
    </section>

    <section class="panneau" style="flex: 1 1 auto;">
      <div class="entete">
        <h2>PR travaillees</h2>
        <div class="droite"><span class="jauge" id="compte">0</span></div>
      </div>
      <div class="cycles" id="cycles"><div class="vide">Aucun cycle.</div></div>
    </section>
  </div>

  <div class="colonne">
    <section class="panneau" style="flex: 1 1 60%;">
      <div class="entete">
        <h2>Le cycle</h2>
        <div class="droite"><span class="cible" id="cible">&mdash;</span></div>
      </div>
      <div class="toile" id="toile">
        <svg id="svg" role="img" aria-label="Le graphe du cycle"></svg>
        <span class="echelle" id="echelle">100 %</span>
        <div class="navig">
          <button id="b-moins" title="Dezoomer">&minus;</button>
          <button id="b-plus" title="Zoomer">+</button>
          <button id="b-ajuster" title="Ajuster a la vue (double-clic sur le plan)">Ajuster</button>
        </div>
      </div>
      <div class="legende">
        <span><i class="puce"></i>pas atteint</span>
        <span><i class="puce fait"></i>traverse</span>
        <span><i class="puce actif"></i>en cours</span>
        <span><i class="puce arret"></i>arret</span>
      </div>
    </section>

    <div class="rangee" style="flex: 1 1 40%;">
      <section class="panneau">
        <div class="entete">
          <h2>En direct</h2>
          <div class="droite"><span class="jauge" id="raison">&mdash;</span></div>
        </div>
        <div class="fil" id="fil"><div class="vide">Rien encore.</div></div>
      </section>

      <section class="panneau">
        <div class="entete">
          <h2>La PR</h2>
          <div class="droite"><span class="jauge" id="pr-cible">&mdash;</span></div>
        </div>
        <div class="pr" id="pr"><div class="vide">Choisir une PR a gauche.</div></div>
      </section>
    </div>
  </div>
</main>

<dialog id="modale">
  <div class="entete">
    <h2>Journal du demon</h2>
    <div class="droite"><button id="b-fermer">Fermer</button></div>
  </div>
  <div class="fil" id="journal"><div class="vide">Rien encore.</div></div>
</dialog>

</div>
<script>
// ── La MISE EN PAGE, et elle seule ──────────────────────────────────────────
//
// Ou tombe chaque noeud est une decision VISUELLE. Ce que SONT les noeuds et les
// arcs vient de `GET /graph`, donc du graphe compile : un schema recopie ici
// derive du cablage a la premiere modification, et rien ne le signale.
//
// Deux mises en page, meme graphe, meme lecture :
//   - la voie LATERALE porte les sorties qui ne passent pas par `settle`, parce
//     qu'aucun bail n'y a ete pris ;
//   - la voie D'ARRET ramene tous les autres arrets vers `settle`. Le dessin dit
//     ainsi ce que le code garantit : quoi qu'il arrive, on conclut.
const VUES = {
  vertical: {
    boite: [128, 40], vue: [440, 800], rail: 366, contour: 118, sens: "v",
    pos: {
      observe: [206,  44], decider: [206, 118], notify:  [ 66, 118],
      admit:   [206, 192], plan:    [206, 266], dry_run: [ 66, 266],
      code:    [206, 358], judge:   [206, 432], verify:  [206, 506],
      publish: [206, 580], speak:   [206, 654], settle:  [206, 748],
    },
  },
  // Recalculee a chaque changement de taille : `vue` et `pos` sont ecrits par
  // `construireCondense`, jamais a la main.
  condense: { boite: [128, 40], vue: [800, 400], pos: {}, cols: 0, sens: "c" },
  horizontal: {
    boite: [128, 40], vue: [1590, 430], rail: 348, contour: 104, sens: "h",
    pos: {
      observe: [ 94, 210], decider: [246, 210], notify:  [246,  74],
      admit:   [398, 210], plan:    [550, 210], dry_run: [550,  74],
      code:    [726, 210], judge:   [878, 210], verify:  [1030, 210],
      publish: [1182, 210], speak:  [1334, 210], settle: [1494, 210],
    },
  },
};

// ── La vue CONDENSEE ────────────────────────────────────────────────────────
//
// Les deux mises en page precedentes sont figees : un graphe 440 x 800 dans un
// panneau large et bas n'occupe qu'un quart de la surface, quoi qu'on fasse.
// Celle-ci se CALCULE — on essaie chaque nombre de colonnes et on garde celui
// qui remplit le mieux le panneau du moment.
//
// L'epine se replie en BOUSTROPHEDON : une rangee vers la droite, la suivante
// vers la gauche. La propriete qui rend ce pliage simple, c'est que le dernier
// noeud d'une rangee et le premier de la suivante tombent dans la MEME colonne
// — le passage d'une rangee a l'autre est donc un trait vertical, pas un
// detour.
const EPINE = ["observe", "decider", "admit", "plan", "code", "judge",
               "verify", "publish", "speak", "settle"];
const DERIVEES = { decider: "notify", plan: "dry_run" };

function construireCondense(w, h) {
  const L = 128, H = 40, gx = 30, gy = 30, gb = 22, marge = 20;
  const bande = H + gb + H;            // voie de derivation, puis epine
  let meilleur = null;
  for (let cols = 2; cols <= EPINE.length; cols++) {
    const rows = Math.ceil(EPINE.length / cols);
    const W = marge * 2 + cols * L + (cols - 1) * gx;
    // `creux` : les courbes d'arret plongent SOUS la derniere rangee. Sans cette
    // reserve, elles depassaient du cadre calcule et se faisaient rogner.
    const creux = 52;
    const Ht = marge * 2 + rows * bande + (rows - 1) * gy + creux;
    const k = Math.min(w / W, h / Ht);
    const score = (W * k) * (Ht * k) / (w * h);
    if (!meilleur || score > meilleur.score) meilleur = { cols, rows, W, Ht, score };
  }

  const { cols, rows, W, Ht } = meilleur;
  const pos = {};
  EPINE.forEach((id, i) => {
    const rang = Math.floor(i / cols);
    const dans = i % cols;
    // Une rangee sur deux se lit a l'envers : c'est ce qui aligne la fin d'une
    // rangee et le debut de la suivante sur la meme colonne.
    const col = rang % 2 === 0 ? dans : cols - 1 - dans;
    const x = marge + col * (L + gx) + L / 2;
    const hautBande = marge + rang * (bande + gy);
    pos[id] = [x, hautBande + H + gb + H / 2];
    if (DERIVEES[id]) pos[DERIVEES[id]] = [x, hautBande + H / 2];
  });

  VUES.condense.pos = pos;
  VUES.condense.vue = [W, Ht];
  VUES.condense.cols = cols;
  return cols;
}

const $ = (s) => document.querySelector(s);
const svg = $("#svg");
const NS = "http://www.w3.org/2000/svg";

let TOPO = null, VUE = "vertical";
const jobs = new Map();
let choisi = null;
const vus = new Set();
let borne = localStorage.getItem("borne") || "";

// La cle de deduplication. Les evenements n'ont pas d'identifiant : trois
// surfaces (`/history`, `/jobs`, le flux SSE) rendent les memes, et sans cle un
// seul cycle s'affiche deux ou trois fois de suite — comme si le demon l'avait
// rejoue.
const empreinte = (e) =>
  [e.ts, e.event, e.job_id || "", e.state || "", (e.why || "").slice(0, 60)].join("|");

// ── Dessin ──────────────────────────────────────────────────────────────────

function el(nom, attrs, parent) {
  const n = document.createElementNS(NS, nom);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}
const cle = (a, b) => a + ">" + b;

function trace(a, b) {
  const v = VUES[VUE], P = v.pos, [L, H] = v.boite;
  const [ax, ay] = P[a], [bx, by] = P[b];
  const dx = L / 2, dy = H / 2;
  const arret = (b === "settle" && a !== "speak");
  const bypass = (a === "judge" && b === "speak");

  // ── Condense : trois regles, et elles suffisent ────────────────────────
  //
  //   meme rangee        -> un trait horizontal
  //   meme colonne       -> un trait vertical (le pli du boustrophedon vers le
  //                         bas, la derivation vers le haut)
  //   tout le reste      -> une courbe qui passe SOUS les deux noeuds
  //
  // La troisieme ne sert qu'aux arrets et au contournement. Ils sont traces
  // faibles et ne s'allument qu'une fois pris : les dessiner tous en clair
  // ferait un plat de spaghettis, les cacher ferait mentir le dessin.
  if (v.sens === "c") {
    if (ay === by) return `M ${ax + (bx > ax ? dx : -dx)} ${ay} H ${bx + (bx > ax ? -dx : dx)}`;
    if (ax === bx) {
      const descend = by > ay;
      return `M ${ax} ${ay + (descend ? dy : -dy)} V ${by + (descend ? -dy : dy)}`;
    }
    const creux = Math.max(46, Math.abs(by - ay) * .45);
    return `M ${ax} ${ay + dy} C ${ax} ${ay + dy + creux}`
         + ` ${bx} ${by + dy + creux} ${bx} ${by + dy}`;
  }

  if (v.sens === "v") {
    if (arret) {
      const [sx, sy] = P.settle, R = v.rail;
      return `M ${ax + dx} ${ay} H ${R - 10} Q ${R} ${ay} ${R} ${ay + 10}`
           + ` V ${sy - 10} Q ${R} ${sy} ${R - 10} ${sy} H ${sx + dx}`;
    }
    if (bypass) {
      const C = v.contour;
      return `M ${ax - dx} ${ay} H ${C + 10} Q ${C} ${ay} ${C} ${ay + 10}`
           + ` V ${by - 10} Q ${C} ${by} ${C + 10} ${by} H ${bx - dx}`;
    }
    if (ax !== bx) return `M ${ax - dx} ${ay} H ${bx + dx}`;
    return `M ${ax} ${ay + dy} V ${by - dy}`;
  }
  if (arret) {
    const [sx, sy] = P.settle, R = v.rail;
    return `M ${ax} ${ay + dy} V ${R - 10} Q ${ax} ${R} ${ax + 10} ${R}`
         + ` H ${sx - 10} Q ${sx} ${R} ${sx} ${R - 10} V ${sy + dy}`;
  }
  if (bypass) {
    const C = v.contour;
    return `M ${ax} ${ay - dy} V ${C + 10} Q ${ax} ${C} ${ax + 10} ${C}`
         + ` H ${bx - 10} Q ${bx} ${C} ${bx} ${C + 10} V ${by - dy}`;
  }
  if (ay !== by) return `M ${ax} ${ay - dy} V ${by + dy}`;
  return `M ${ax + dx} ${ay} H ${bx - dx}`;
}

function dessiner() {
  if (!TOPO) return;
  const v = VUES[VUE], [L, H] = v.boite;
  dimensionner();
  svg.textContent = "";

  const defs = el("defs", {}, svg);
  // La grille : un reperage discret qui donne une echelle au dessin sans attirer
  // l'oeil. Deux pas, fin et gros, comme un papier millimetre.
  const p1 = el("pattern", { id: "g1", width: 22, height: 22, patternUnits: "userSpaceOnUse" }, defs);
  el("path", { d: "M 22 0 L 0 0 0 22", fill: "none", stroke: "rgba(33,55,66,.30)", "stroke-width": .6 }, p1);
  const p2 = el("pattern", { id: "g2", width: 110, height: 110, patternUnits: "userSpaceOnUse" }, defs);
  el("rect", { width: 110, height: 110, fill: "url(#g1)" }, p2);
  el("path", { d: "M 110 0 L 0 0 0 110", fill: "none", stroke: "rgba(33,55,66,.55)", "stroke-width": .8 }, p2);
  const cam = el("g", { id: "cam" }, svg);
  // La grille est DANS la camera : elle se deplace et se redimensionne avec le
  // graphe. Une grille fixe sous un dessin qui bouge donne l'impression que
  // c'est le fond qui glisse, pas le plan qu'on manipule. Assez large pour
  // qu'aucun deplacement raisonnable n'en revele le bord.
  el("rect", { x: -4000, y: -4000, width: 12000, height: 12000, fill: "url(#g2)" }, cam);

  const m = el("marker", { id: "fleche", viewBox: "0 0 8 8", refX: "6.5", refY: "4",
    markerWidth: "5.5", markerHeight: "5.5", orient: "auto-start-reverse" }, defs);
  el("path", { d: "M 0 .8 L 8 4 L 0 7.2 z", fill: "currentColor" }, m);

  const gArcs = el("g", {}, cam);
  for (const e of TOPO.edges) {
    const A = v.pos[e.source], B = v.pos[e.target];
    if (!A || !B) continue;
    // « Libre » = ni meme rangee ni meme colonne, donc trace en courbe : les
    // arrets et le contournement. Ils s'affichent faibles tant qu'on ne les a
    // pas pris.
    const libre = v.sens === "c" && A[0] !== B[0] && A[1] !== B[1];
    el("path", { d: trace(e.source, e.target), "marker-end": "url(#fleche)",
      class: "arc" + (e.conditional ? " conditionnel" : "") + (libre ? " libre" : ""),
      "data-arc": cle(e.source, e.target) }, gArcs);
  }

  for (const n of TOPO.nodes) {
    const p = v.pos[n.id];
    if (!p) continue;
    const [x, y] = p;
    const g = el("g", { class: "noeud", "data-noeud": n.id }, cam);
    el("rect", { x: x - L / 2, y: y - H / 2, width: L, height: H, rx: 10, class: "halo" }, g);
    el("rect", { x: x - L / 2, y: y - H / 2, width: L, height: H, rx: 10, class: "corps" }, g);
    el("text", { x, y: y - 2, "text-anchor": "middle", class: "titre" }, g).textContent = n.label;
    el("text", { x, y: y + 11, "text-anchor": "middle", class: "note" }, g).textContent = n.detail;
    el("title", {}, g).textContent = n.id + " \\u2014 " + n.detail;
  }
  // Un dessin neuf se recadre : changer d'orientation sans reajuster
  // laisserait la camera sur un cadrage calcule pour l'autre mise en page.
  ajuster();
  peindre();
}

// ── La camera ───────────────────────────────────────────────────────────────
//
// Le SVG occupe tout le panneau et son `viewBox` vaut sa taille EN PIXELS : un
// point du plan vaut donc un pixel a l'echelle 1, et les coordonnees de la
// souris se lisent sans conversion. Tout le cadrage est porte par la
// transformation du groupe `#cam`.
//
// C'est ce qui repare le defaut du premier jet : le SVG se dimensionnait sur son
// `viewBox`, donc un graphe horizontal (1590 x 430) rendu dans un panneau de
// 880 x 750 occupait 240 px de haut — un quart de la surface, et rien a faire
// des trois autres quarts.
let CAM = { k: 1, tx: 0, ty: 0 };
let VUEW = 800, VUEH = 600, ajustee = true;

function dimensionner() {
  const boite = $("#toile");
  VUEW = Math.max(1, boite.clientWidth);
  VUEH = Math.max(1, boite.clientHeight);
  svg.setAttribute("viewBox", `0 0 ${VUEW} ${VUEH}`);
  // La vue condensee depend de la taille : elle se recalcule ici, avant que
  // `dessiner` ou `ajuster` ne lisent ses positions.
  if (VUE === "condense") return construireCondense(VUEW, VUEH);
  return 0;
}

function appliquer() {
  const cam = svg.querySelector("#cam");
  if (cam) cam.setAttribute("transform", `translate(${CAM.tx} ${CAM.ty}) scale(${CAM.k})`);
  $("#echelle").textContent = Math.round(CAM.k * 100) + " %";
}

function ajuster() {
  const [W, H] = VUES[VUE].vue;
  const k = Math.min(VUEW / W, VUEH / H) * .94;
  CAM = { k, tx: (VUEW - W * k) / 2, ty: (VUEH - H * k) / 2 };
  ajustee = true;
  appliquer();
}

function zoomer(f, cx, cy) {
  // Zoom AU CURSEUR : le point sous la souris ne bouge pas. Zoomer au centre
  // oblige a repositionner apres chaque cran, et on perd ce qu'on regardait.
  const k = Math.min(4, Math.max(.15, CAM.k * f));
  const r = k / CAM.k;
  CAM = { k, tx: cx - (cx - CAM.tx) * r, ty: cy - (cy - CAM.ty) * r };
  ajustee = false;
  appliquer();
}

svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const b = svg.getBoundingClientRect();
  zoomer(e.deltaY < 0 ? 1.13 : 1 / 1.13, e.clientX - b.left, e.clientY - b.top);
}, { passive: false });

let tire = null;
svg.addEventListener("pointerdown", (e) => {
  // Coupe le glisser-selectionner du navigateur des le premier appui. Le CSS
  // suffit pour les navigateurs recents ; ceci couvre le reste, et empeche
  // aussi le fantome de glisser-deposer sur les elements graphiques.
  e.preventDefault();
  tire = { x: e.clientX, y: e.clientY, tx: CAM.tx, ty: CAM.ty };
  svg.setPointerCapture(e.pointerId);
  svg.classList.add("tire");
});
svg.addEventListener("pointermove", (e) => {
  if (!tire) return;
  CAM.tx = tire.tx + (e.clientX - tire.x);
  CAM.ty = tire.ty + (e.clientY - tire.y);
  ajustee = false;
  appliquer();
});
for (const fin of ["pointerup", "pointercancel", "pointerleave"]) {
  svg.addEventListener(fin, () => { tire = null; svg.classList.remove("tire"); });
}
svg.addEventListener("dblclick", ajuster);

// Au redimensionnement, on ne RECADRE que si personne n'a touche au cadrage.
// Reajuster par-dessus un zoom manuel ferait perdre ce qu'on etait en train de
// regarder chaque fois que la fenetre bouge d'un pixel.
new ResizeObserver(() => {
  const avant = VUES.condense.cols;
  const cols = dimensionner();
  // En condense, un changement de taille peut changer le nombre de COLONNES :
  // il faut alors redessiner, pas seulement recadrer. Redessiner a chaque pixel
  // serait du gaspillage — on ne le fait que quand le pliage change vraiment.
  if (VUE === "condense" && cols !== avant) { dessiner(); return; }
  if (ajustee) ajuster(); else appliquer();
}).observe($("#toile"));


// ── Peinture de l'etat ──────────────────────────────────────────────────────

function peindre() {
  for (const g of svg.querySelectorAll(".noeud")) g.setAttribute("class", "noeud");
  for (const a of svg.querySelectorAll(".arc")) {
    a.setAttribute("class", "arc"
      + (a.classList.contains("conditionnel") ? " conditionnel" : "")
      + (a.classList.contains("libre") ? " libre" : ""));
  }
  const j = choisi ? jobs.get(choisi) : null;
  if (!j) return;
  const fini = j.statut !== "en_cours";
  const rate = j.statut === "echec";

  j.chemin.forEach((n, i) => {
    const g = svg.querySelector(`[data-noeud="${n}"]`);
    if (!g) return;
    const dernier = i === j.chemin.length - 1;
    let c = "noeud fait";
    if (dernier && !fini) c = "noeud actif";
    else if (dernier && rate) c = "noeud arret fin";
    else if (dernier) c = "noeud fait fin";
    g.setAttribute("class", c);
    if (i > 0) {
      const arc = svg.querySelector(`[data-arc="${cle(j.chemin[i - 1], n)}"]`);
      if (arc) {
        const garde = (arc.classList.contains("conditionnel") ? " conditionnel" : "")
                    + (arc.classList.contains("libre") ? " libre" : "");
        arc.setAttribute("class", "arc" + garde + ((dernier && !fini) ? " encours" : " pris"));
      }
    }
  });
}

// ── Liste des cycles ────────────────────────────────────────────────────────

const MOT = {
  en_cours: "en cours", termine: "termine", attente: "arbitrage",
  echec: "echec", a_blanc: "a blanc", interrompu: "interrompu", test: "test",
};

function quand(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return "";
  const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  return d.toDateString() === new Date().toDateString()
    ? hm : String(d.getDate()).padStart(2, "0") + "/"
         + String(d.getMonth() + 1).padStart(2, "0") + " " + hm;
}

function visibles() {
  return [...jobs.values()]
    .filter((j) => !borne || j.test || j.statut === "en_cours" || (j.debut || "") > borne)
    .sort((a, b) => (b.debut || "").localeCompare(a.debut || ""));
}

// Les cycles d'une MEME PR, du plus recent au plus ancien.
//
// `visibles()` rend un cycle par passage. Sur une PR que le demon reprend
// toutes les cinq minutes, ca faisait 120 lignes identiques — et le travail
// reel des autres depots enseveli dessous. Ce qu'on veut savoir d'une PR,
// c'est son dernier etat et QUAND.
function grouper(liste) {
  const par = new Map();
  for (const j of liste) {
    const cle = j.repo + "#" + j.pr;
    if (!par.has(cle)) par.set(cle, []);
    par.get(cle).push(j);
  }
  // `visibles()` est deja trie du plus recent au plus ancien : le premier de
  // chaque groupe est donc le dernier cycle, et l'ordre des groupes suit celui
  // de leur cycle le plus recent.
  return [...par.entries()].map(([cle, cycles]) => ({cle, cycles}));
}

function lister() {
  const boite = $("#cycles"), groupes = grouper(visibles());
  // Le compteur porte le nombre de PR, pas de passages : c'est ce que la liste
  // montre desormais, et deux nombres differents pour une meme liste se lisent
  // comme un bug.
  $("#compte").textContent = groupes.length;
  boite.textContent = "";
  if (!groupes.length) { boite.innerHTML = '<div class="vide">Aucun cycle.</div>'; return; }
  for (const g of groupes) {
    const j = g.cycles[0];                 // le plus recent
    const st = j.test ? "test" : j.statut;
    const encours = g.cycles.some((x) => x.statut === "en_cours");
    const choisiIci = g.cycles.some((x) => x.id === choisi);
    const b = document.createElement("button");
    b.className = "cycle" + (encours ? " vif" : "");
    b.setAttribute("aria-selected", String(choisiIci));
    // Le NOMBRE reste visible : vingt passages sur une PR n'est pas la meme
    // situation qu'un seul, et c'est ce qu'on veut voir sans compter des lignes.
    const combien = g.cycles.length > 1
      ? `<span class="fois">&times;${g.cycles.length}</span>` : "";
    b.innerHTML =
      `<div class="haut"><span class="depot">${ech(g.cle)}</span>${combien}`
      + `<span class="quand">${quand(j.debut)}</span></div>`
      + `<div class="bas"><span class="statut s-${ech(st)}">${ech(MOT[st] || st)}</span>`
      + `<span class="etapes">${j.chemin.length}/12</span></div>`;
    // Ouvre le DERNIER cycle : c'est celui qu'on regarde quand on se demande
    // ce qui se passe maintenant.
    b.onclick = () => { choisi = j.id; charger(j); };
    boite.appendChild(b);
  }
}

async function charger(j) {
  // Un cycle relu depuis l'historique n'a que son resume : son fil complet se
  // demande a l'ouverture. Le charger pour TOUS d'un coup ferait des dizaines
  // de requetes pour des cycles que personne ne regardera.
  if (!j.etapes.length && !j.test) {
    const d = await jget("/history/" + j.id);
    if (d) {
      j.etapes = d.events;
      // DECLARER ce qu'on vient de charger. `absorber` deduplique sur `vus` ;
      // sans cette boucle, les memes evenements reviennent par le flux SSE et
      // par `/jobs` et le fil les empile une seconde fois — c'est le reste du
      // doublement d'affichage, celui qui survivait a la premiere correction.
      for (const e of d.events) vus.add(empreinte(e));
    }
  }
  peindre(); lister(); filer();
}

// ── Les PR suivies ──────────────────────────────────────────────────────────
//
// `/jobs` ne montre que le TRAVAIL. Une decision « rien a faire » n'en produit
// aucun, et la console affichait alors « aucun cycle » — un demon mort a
// l'ecran, alors qu'il tournait et avait quelque chose a dire.
//
// « Rien a faire » et « rien vu » sont deux etats differents.
const ETATS = {
  NEEDS_FIX: "correction requise", AGENT_WORKING: "en cours",
  WAITING_CI: "attente CI", WAITING_REVIEW: "attente revue",
  NEEDS_HUMAN: "arbitrage requis", READY_FOR_HUMAN: "pr\u00eate \u00e0 merger",
  IDLE: "aucune action",
};

// La derniere photo servie par /pulls, indexee par depot#numero : le panneau
// de droite la relit sans redemander la forge.
const PHOTOS = new Map();
// Les PR qu'un job tient EN CE MOMENT, d'apres les baux — le seul etat reel.
const ACTIFS = new Set();
let prChoisie = null;

// Le corps d'un fil vient de la FORGE : il est ecrit par des tiers. L'injecter
// tel quel dans `innerHTML` donnerait a un commentaire de PR le droit
// d'executer du script dans la console du demon — laquelle sait declencher un
// balayage et reprendre une PR.
function ech(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Reprendre une PR que le demon laisse de cote. Ne leve QUE les deux verrous
// qui attendent une personne — cycles epuises, question sans reponse. Le bail,
// les branches partagees, les verifications et le plafond du jour tiennent.
async function reprendre(p, bouton) {
  const texte = bouton.textContent;
  bouton.disabled = true; bouton.textContent = "\u2026";
  let mot = "?", genre = "sweep.echec", pourquoi = "";
  try {
    const r = await fetch("/forcer/" + encodeURIComponent(p.profile) + "/"
      + encodeURIComponent(p.repository) + "/" + p.pull_request, {method: "POST"});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { mot = "refuse"; pourquoi = d.detail || String(r.status); }
    else { mot = "repris"; pourquoi = d.raison || ""; genre = "sweep.demande"; }
  } catch (e) { mot = "hors ligne"; pourquoi = String(e.message || e); }
  journaliser({event: genre, ts: new Date().toISOString(), why: pourquoi});
  bouton.textContent = mot;
  setTimeout(() => { bouton.textContent = texte; bouton.disabled = false; }, 2600);
}

// Le detail de la PR choisie. Tout vient de la photo deja servie par /pulls :
// l'afficher ne coute pas un appel de plus a la forge.
function montrerPR(p) {
  const boite = $("#pr"), cible = $("#pr-cible");
  if (!p) {
    cible.textContent = "\u2014";
    boite.innerHTML = `<div class="vide">Choisir une PR a gauche.</div>`;
    return;
  }
  const cle = p.repository + "#" + p.pull_request;
  cible.textContent = cle;
  const nom = p.url
    ? `<a class="lien" href="${ech(p.url)}" target="_blank" rel="noopener">${ech(cle)}</a>`
    : ech(cle);
  const L = [];
  L.push(`<div class="ligne"><span class="cle">PR</span><span class="val">${nom}`
    + (p.brouillon ? ` <span class="qui">(brouillon)</span>` : "") + `</span></div>`);
  L.push(`<div class="ligne"><span class="cle">Branche</span><span class="val">`
    + `${ech(p.titre || "\u2014")}${p.base ? " \u2192 " + ech(p.base) : ""}</span></div>`);
  L.push(`<div class="ligne"><span class="cle">Auteur</span><span class="val">`
    + `${ech(p.auteur || "\u2014")}</span></div>`);
  L.push(`<div class="ligne"><span class="cle">Etat</span><span class="val">`
    + `${ech(ETATS[p.etat] || p.etat)}${p.cycle ? " \u00b7 cycle " + p.cycle : ""}</span></div>`);
  L.push(`<div class="ligne"><span class="cle">Decision</span><span class="val">`
    + `${ech(p.raison || "")}</span></div>`);

  const checks = p.checks || [];
  L.push(`<div class="bloc">V\u00e9rifications (${checks.length})</div>`);
  if (p.checks_lisibles === false) {
    L.push(`<div class="msg">Illisibles : droits du jeton, ou API indisponible. `
      + `Le demon ne peut pas distinguer une CI verte d'une CI qu'il ne voit pas.</div>`);
  } else if (!checks.length) {
    L.push(`<div class="msg">Aucune.</div>`);
  } else {
    for (const c of checks) {
      const bon = c.verdict === "success";
      const ko = c.verdict === "failure" || c.verdict === "timed_out"
        || c.verdict === "cancelled";
      L.push(`<div class="chk"><i class="puce2 ${bon ? "ok" : (ko ? "ko" : "")}"></i>`
        + `${ech(c.nom)} <span class="qui">${ech(c.verdict || c.etat || "")}</span></div>`);
    }
  }

  const fils = p.fils_detail || [];
  L.push(`<div class="bloc">Fils de revue (${fils.length})</div>`);
  if (!fils.length) L.push(`<div class="msg">Aucun.</div>`);
  for (const f of fils) {
    const cls = "item" + (f.attente ? " attente" : "") + (f.resolu ? " resolu" : "");
    const ou = f.fichier
      ? ech(f.fichier) + (f.ligne ? ":" + f.ligne : "")
      : "sans ancrage de fichier";
    const etiq = f.attente ? ` <span class="qui">\u00b7 attend une r\u00e9ponse</span>`
      : (f.resolu ? ` <span class="qui">\u00b7 r\u00e9solu</span>` : "");
    const msgs = (f.messages || []).map(
      (m) => `<div class="msg"><span class="qui">${ech(m.auteur)}</span> `
        + `${ech(m.corps)}</div>`).join("");
    L.push(`<div class="${cls}"><div class="ou">${ou}${etiq}</div>${msgs}</div>`);
  }
  boite.innerHTML = L.join("");
}

async function chargerPulls() {
  const d = await jget("/pulls");
  const boite = $("#pulls");
  const liste = d && d.pulls ? d.pulls : [];
  $("#balaye").textContent = d && d.balaye_a ? quand(d.balaye_a) : "\u2014";
  boite.textContent = "";
  PHOTOS.clear();
  if (!liste.length) {
    boite.innerHTML = `<div class="vide">${ech(
      d && d.raison ? d.raison
        : (d && d.balaye_a ? "Aucune PR ouverte dans le perimetre."
                           : "Pas encore balaye."))}</div>`;
    montrerPR(null);
    return;
  }
  for (const p of liste) {
    const cle = p.repository + "#" + p.pull_request;
    PHOTOS.set(cle, p);
    // Une carte n'est plus un <button> : elle porte un lien vers la forge et,
    // quand la PR est bloquee, un bouton de reprise. Un bouton dans un bouton
    // n'est pas du HTML valide.
    const b = document.createElement("div");
    b.setAttribute("role", "button");
    b.tabIndex = 0;
    // Une PR qui ATTEND quelque chose de nous doit se distinguer d'une PR
    // qu'on regarde passer : c'est la seule information qui appelle un geste.
    const vif = p.etat === "AGENT_WORKING" || p.etat === "NEEDS_FIX";
    b.className = "cycle" + (vif ? " vif" : "");
    const nom = p.url
      ? `<a class="lien" href="${ech(p.url)}" target="_blank" rel="noopener" `
        + `title="Ouvrir sur la forge">${ech(cle)}</a>`
      : ech(cle);
    // Le bouton n'apparait QUE la ou il fait quelque chose. Le forcage ne leve
    // que les verrous qui attendent une personne ; ailleurs il donnerait
    // l'illusion d'un geste sans effet.
    // Pas de bouton pendant qu'un job tient la PR. La carte montre la photo
    // du balayage PRECEDENT — pendant un job elle affiche encore « arbitrage
    // requis » — donc sans ce test l'interface invite a un geste que l'etat
    // reel rend absurde. Le serveur refuse aussi : une page ouverte depuis dix
    // minutes ne sait rien du bail.
    const occupe = ACTIFS.has(cle);
    const reprise = (p.etat === "NEEDS_HUMAN" && !occupe)
      ? `<button class="reprendre" title="Passe outre l'attente d'arbitrage `
        + `et les cycles epuises. Ne touche ni au bail, ni aux verifications, `
        + `ni au plafond du jour.">Reprendre</button>`
      : "";
    b.innerHTML =
      `<div class="haut"><span class="depot">${nom}</span>`
      + `<span class="quand">${p.cycle ? "cycle " + p.cycle : ""}</span></div>`
      + `<div class="bas"><span class="statut s-${ech(p.etat)}">`
      + `${ech(ETATS[p.etat] || p.etat)}</span>`
      + `<span class="etapes">${p.fils ? p.fils + " fil(s)" : ""}</span>`
      + reprise + `</div>`
      + `<div class="pourquoi">${ech(p.raison || "")}</div>`;
    b.onclick = (ev) => {
      // Le lien et le bouton passent d'abord : cliquer « Reprendre » ne doit
      // pas aussi changer la selection sous les doigts.
      if (ev.target.closest("a, button")) return;
      prChoisie = cle;
      montrerPR(p);
      const j = visibles().find(
        (x) => x.repo === p.repository && x.pr === p.pull_request);
      if (j) { choisi = j.id; charger(j); }
    };
    const rb = b.querySelector(".reprendre");
    if (rb) rb.onclick = (ev) => { ev.stopPropagation(); reprendre(p, rb); };
    boite.appendChild(b);
  }
  // La photo a change sous nos yeux : garder celle qu'on regardait si elle est
  // toujours la, sinon montrer la premiere plutot qu'un panneau vide.
  const vue = prChoisie ? PHOTOS.get(prChoisie) : null;
  montrerPR(vue || liste[0]);
  if (!vue) prChoisie = liste[0].repository + "#" + liste[0].pull_request;
}


// ── Fil ─────────────────────────────────────────────────────────────────────

const QUOI = {
  "graph.node": ["noeud", "noeud-fil"], "agent.step": ["agent", "etape"],
  "job.check": ["check", ""], "job.started": ["depart", ""],
  "job.finished": ["fini", "bon"], "job.needs_human": ["arret", "mauvais"],
  "job.dry_run": ["a blanc", ""], "job.moteur": ["moteur", ""],
  "sweep.done": ["balayage", ""], "sweep.decision": ["decide", ""],
  "lease.reclaimed": ["bail", ""], "notify.dry_run": ["muet", ""],
  "job.diff": ["diff", "bon"],
  "sweep.demande": ["balayage", "bon"], "sweep.refuse": ["balayage", ""],
  "sweep.echec": ["balayage", "mauvais"],
};
const heure = (ts) => (ts || "").slice(11, 19);

function ligne(e) {
  let [mot, style] = QUOI[e.event] || [e.event.split(".").pop(), ""];
  // Ce que l'agent DIT n'est pas ce qu'il LANCE. Sous la meme etiquette, une
  // phrase de raisonnement se lit comme une commande.
  if (e.event === "agent.step" && e.state === "texte") { mot = "dit"; style = "dit"; }

  const d = document.createElement("div");
  d.className = "ligne " + style;
  const h = document.createElement("span"); h.className = "h"; h.textContent = heure(e.ts);
  const q = document.createElement("span"); q.className = "quoi"; q.textContent = mot;
  const t = document.createElement("span"); t.className = "txt";
  if (e.event === "graph.node") {
    const n = TOPO && TOPO.nodes.find((x) => x.id === e.state);
    t.textContent = n ? `${n.label} \\u2014 ${n.detail}` : e.state;
  } else {
    t.textContent = e.why || e.result || e.state || "";
  }
  d.append(h, q, t);

  // Le DETAIL : sortie d'un check rouge, fichiers d'un diff, anomalies d'un
  // verdict. Il etait deja dans le journal et rien ne l'affichait — il fallait
  // rouvrir le fichier sur disque pour lire ce que la commande avait dit.
  // Replie par defaut : deploye, il noierait la sequence.
  const detail = detailLisible(e);
  if (detail) {
    d.classList.add("pliable");
    const p = document.createElement("pre");
    p.className = "detail";
    p.textContent = detail;
    d.appendChild(p);
    d.onclick = () => d.classList.toggle("ouvert");
  }
  return d;
}

// Ce qu'on sait rendre LISIBLE. Le reste du `detail` est une structure interne :
// l'afficher brute donnerait du JSON a lire a quelqu'un qui cherche pourquoi un
// test a echoue.
function detailLisible(e) {
  const d = e.detail;
  if (!d) return "";
  if (typeof d.tail === "string" && d.tail.trim()) return d.tail.trim();
  if (Array.isArray(d.files) && d.files.length) return d.files.join("\\n");
  if (Array.isArray(d.anomalies) && d.anomalies.length) return d.anomalies.join("\\n");
  return "";
}

function filer() {
  const boite = $("#fil"), j = choisi ? jobs.get(choisi) : null;
  $("#cible").textContent = j ? `${j.repo}#${j.pr}` : "\\u2014";
  $("#raison").textContent = j && j.raison ? j.raison.slice(0, 88) : "\\u2014";
  boite.textContent = "";
  if (!j || !j.etapes.length) { boite.innerHTML = '<div class="vide">Rien encore.</div>'; return; }
  for (const e of j.etapes.slice(-300)) boite.appendChild(ligne(e));
  boite.scrollTop = boite.scrollHeight;
}

// ── Reception ───────────────────────────────────────────────────────────────
//
// DEDUPLIQUE, et ce n'est pas du confort. Le flux SSE rejoue ses derniers
// evenements a la connexion, `/jobs` rend les memes, et `/history` couvre la
// meme periode : sans cle, un cycle apparaissait DEUX OU TROIS FOIS de suite
// dans le fil — comme si le demon l'avait rejoue.
const FINS = { "job.finished": 1, "job.needs_human": "echec", "job.dry_run": "a_blanc" };

function absorber(e, opts) {
  const k = empreinte(e);
  if (vus.has(k)) return;
  vus.add(k);

  if (!e.job_id) return journaliser(e);
  let j = jobs.get(e.job_id);
  if (!j) {
    j = { id: e.job_id, repo: e.repository || "?", pr: e.pull_request || 0,
          chemin: [], statut: "en_cours", debut: e.ts, raison: "", etapes: [] };
    jobs.set(e.job_id, j);
    if (!(opts && opts.historique)
        && (!choisi || (jobs.get(choisi) || {}).statut !== "en_cours")) choisi = e.job_id;
  }
  // Un evenement anterieur a la borne est DEJA dans le chemin rendu par
  // `/history`. Le garde « pas deux fois de suite » ne suffit pas : une sequence
  // rejouee recommence par `observe` alors que le dernier noeud connu est
  // `dry_run`, et rien ne s'y oppose.
  const deja = j.borne && e.ts && e.ts <= j.borne;
  if (e.event === "graph.node" && e.state && !deja
      && j.chemin[j.chemin.length - 1] !== e.state) {
    j.chemin.push(e.state);
  }
  const fin = FINS[e.event];
  if (fin) {
    j.statut = fin === 1 ? (e.state === "NEEDS_HUMAN" ? "attente" : "termine") : fin;
    j.raison = e.why || j.raison;
  }
  j.etapes.push(e);
  if (e.job_id === choisi) { peindre(); filer(); }
  lister();
  journaliser(e);
}

function journaliser(e) {
  if (e.event === "agent.step" || e.event === "graph.node") return;
  const boite = $("#journal");
  const premier = boite.firstElementChild;
  if (premier && premier.className === "vide") boite.textContent = "";
  boite.appendChild(ligne(e));
  while (boite.childElementCount > 200) boite.removeChild(boite.firstElementChild);
}

// ── Simulation ──────────────────────────────────────────────────────────────
//
// Un cycle FAUX, entierement dans le navigateur. Il n'appelle rien, n'ecrit
// rien, et porte le statut « test » : une demonstration qui ressemble a la
// production finit par tromper quelqu'un.
const SCENARIO = [
  ["observe", 480], ["decider", 560], ["admit", 460], ["plan", 680],
  ["code", 700], ["judge", 760], ["verify", 820], ["publish", 660],
  ["speak", 560], ["settle", 620],
];
const OUTILS = [
  "Read src/app/observations/index.tsx", "Grep useObservationFilters",
  "Edit src/app/observations/index.tsx", "Bash npm run lint",
  "Read src/shared/api/client.ts", "Edit src/shared/api/client.ts",
  "Bash npm run test:ci",
];
let nTest = 0, testEnCours = false;
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

async function simuler() {
  if (testEnCours) return;
  testEnCours = true;
  $("#b-test").classList.add("vif");
  const id = "test-" + (++nTest);
  const j = { id, repo: "demo", pr: 900 + nTest, chemin: [], statut: "en_cours",
              debut: new Date().toISOString(), raison: "cycle simule", etapes: [], test: true };
  jobs.set(id, j);
  choisi = id;
  const pose = (e) => {
    e.ts = new Date().toISOString(); e.job_id = id;
    j.etapes.push(e);
    if (choisi === id) filer();
  };
  for (const [noeud, duree] of SCENARIO) {
    j.chemin.push(noeud);
    pose({ event: "graph.node", state: noeud });
    peindre(); lister();
    if (noeud === "code") {
      for (const outil of OUTILS) { await pause(240); pose({ event: "agent.step", why: outil }); }
    }
    await pause(duree);
  }
  j.statut = "termine";
  j.raison = "3 fichier(s) modifie(s) \\u2014 2 corrige(s), 0 en attente d'arbitrage";
  pose({ event: "job.finished", state: "WAITING_CI", why: j.raison });
  peindre(); lister();
  $("#b-test").classList.remove("vif");
  testEnCours = false;
}

// ── Chargement ──────────────────────────────────────────────────────────────

async function jget(u) { try { const r = await fetch(u); return r.ok ? r.json() : null; } catch { return null; } }

// L'orientation qui remplit le mieux le panneau disponible.
//
// Un graphe vertical (440 x 800) dans un panneau large et bas n'occupe qu'un
// quart de la surface : le rapport de forme du dessin et celui du panneau ne se
// rencontrent jamais. Plutot que d'imposer un defaut, on prend celui des deux
// qui remplit le plus — et l'utilisateur reste maitre des qu'il choisit.
function orienter(vue, { choix = false } = {}) {
  VUE = vue;
  $("#b-cond").setAttribute("aria-pressed", String(vue === "condense"));
  $("#b-vert").setAttribute("aria-pressed", String(vue === "vertical"));
  $("#b-hori").setAttribute("aria-pressed", String(vue === "horizontal"));
  // On ne RETIENT que les choix explicites. Memoriser l'orientation
  // automatique la figerait des le premier chargement, et la fenetre aurait
  // beau changer de forme, le graphe resterait dans celle d'hier.
  if (choix) localStorage.setItem("vue", vue);
  dessiner();
}

async function demarrer() {
  TOPO = await jget("/graph");
  // Le condense est le defaut : il se calcule pour la place disponible, la ou
  // les deux autres subissent le rapport de forme du panneau.
  orienter(localStorage.getItem("vue") || "condense");

  const sante = await jget("/health");
  if (sante) {
    const a = $("#armement");
    a.textContent = sante.writes_enabled ? "mode \u00e9criture" : "lecture seule";
    a.className = "jauge" + (sante.writes_enabled ? " arme" : "");
    $("#parallele").textContent = sante.max_parallel;
  }

  const hist = await jget("/history");
  for (const h of (hist ? hist.jobs : [])) {
    jobs.set(h.job_id, {
      id: h.job_id, repo: h.repository || "?", pr: h.pull_request || 0,
      chemin: h.chemin || [], statut: h.statut, debut: h.debut,
      raison: h.raison || "", etapes: [],
      // La BORNE de ce que le serveur a deja compte. Le flux SSE rejoue ses
      // derniers evenements et `/jobs` rend les memes : sans elle, les noeuds
      // deja dans `chemin` s'y rempilent, et un cycle de 5 noeuds s'affiche a 10.
      borne: h.fin || h.debut || "",
    });
  }
  const liste = visibles();
  const vise = liste.find((j) => j.statut === "en_cours") || liste[0];
  if (vise) { choisi = vise.id; await charger(vise); } else { lister(); }

  const etat = await jget("/jobs");
  if (etat) {
    $("#actifs").textContent = etat.active.length;
    ACTIFS.clear();
    for (const b of etat.active) ACTIFS.add(b.repository + "#" + b.pull_request);
    for (const e of etat.recent) absorber(e, { historique: true });
  }
  await chargerPulls();

  const flux = new EventSource("/events");
  flux.onopen = () => $("#pouls").classList.add("vif");
  flux.onerror = () => $("#pouls").classList.remove("vif");
  flux.onmessage = (m) => {
    let e; try { e = JSON.parse(m.data); } catch { return; }
    absorber(e);
    if (FINS[e.event] || e.event === "job.started") {
      jget("/jobs").then((s) => { if (s) $("#actifs").textContent = s.active.length; });
    }
    // Un balayage vient de finir : ce qu'il a vu a change.
    if (e.event === "sweep.done") chargerPulls();
  };
}

$("#b-moins").onclick = () => zoomer(1 / 1.3, VUEW / 2, VUEH / 2);
$("#b-plus").onclick = () => zoomer(1.3, VUEW / 2, VUEH / 2);
$("#b-ajuster").onclick = ajuster;
$("#b-cond").onclick = () => orienter("condense", { choix: true });
$("#b-vert").onclick = () => orienter("vertical", { choix: true });
$("#b-hori").onclick = () => orienter("horizontal", { choix: true });
// Relire la forge MAINTENANT. Ce bouton ne change aucun reglage : il ne dit pas
// ce que le demon a le droit de faire, il dit quand il fait ce qu'il ferait de
// toute facon. Demon arme, un balayage peut lancer un agent tout de suite — la
// reponse le DIT, plutot que de laisser croire a un rafraichissement d'affichage.
$("#b-balayer").onclick = async () => {
  const b = $("#b-balayer"), texte = b.textContent;
  b.disabled = true; b.classList.add("vif"); b.textContent = "\u2026";
  let mot = "?", genre = "sweep.echec", pourquoi = "";
  try {
    const r = await fetch("/sweep", {method: "POST"});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { mot = "refuse"; pourquoi = d.detail || `${r.status}`; genre = "sweep.echec"; }
    else if (d.lance) { mot = "lance"; pourquoi = d.raison || ""; genre = "sweep.demande"; }
    else { mot = "en cours"; pourquoi = d.raison || ""; genre = "sweep.refuse"; }
  } catch (e) { mot = "hors ligne"; pourquoi = String(e.message || e); }
  journaliser({event: genre, ts: new Date().toISOString(), why: pourquoi});
  b.textContent = mot;
  setTimeout(() => {
    b.textContent = texte; b.disabled = false; b.classList.remove("vif");
  }, 2600);
};

$("#b-test").onclick = simuler;
$("#b-journal").onclick = () => $("#modale").showModal();
$("#b-fermer").onclick = () => $("#modale").close();
$("#b-vider").onclick = () => {
  // MASQUE, n'efface pas. Les fichiers de journal sont la memoire du demon : une
  // console capable de les supprimer serait une console capable de faire
  // disparaitre la preuve d'un incident. La borne est retenue localement.
  borne = new Date().toISOString();
  localStorage.setItem("borne", borne);
  for (const [id, j] of [...jobs]) if (j.statut !== "en_cours" && !j.test) jobs.delete(id);
  choisi = (visibles()[0] || {}).id || null;
  $("#journal").innerHTML = '<div class="vide">Rien encore.</div>';
  peindre(); lister(); filer();
};

demarrer();
</script>
</body>
</html>
"""
