"""La page d'installation. Servie tant qu'il n'y a pas de configuration.

Trois etapes, et l'ordre compte : on ne demande de cocher des depots qu'APRES
avoir verifie le jeton contre la forge. Faire saisir une configuration entiere
pour decouvrir a la fin que le jeton est mort, c'est faire recommencer.

Meme palette que la console : ce sont deux moments d'un meme outil.
"""

from __future__ import annotations

__all__ = ["PAGE_SETUP"]

PAGE_SETUP = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>reviewer — installation</title>
<style>
  :root {
    color-scheme: dark;
    --vide:#04070A; --panneau:#0A1218; --trait:#16262E; --trait2:#213742;
    --encre:#E2F5F0; --doux:#8AA5AE; --pale:#56717C;
    --neon:#2BE8B0; --cyan:#45D2FF; --violet:#9D7BFF; --ambre:#FFC061; --rouge:#FF6E7E;
    --sans:"Segoe UI Variable Text","Segoe UI",Inter,system-ui,-apple-system,sans-serif;
    --mono:"Cascadia Mono","JetBrains Mono",ui-monospace,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{
    margin:0;min-height:100vh;background:var(--vide);color:var(--encre);
    font:14px/1.6 var(--sans);-webkit-font-smoothing:antialiased;
    display:flex;justify-content:center;padding:38px 20px 60px;
  }
  body::before{
    content:"";position:fixed;inset:0;pointer-events:none;
    background:
      radial-gradient(60vw 42vh at 18% 0%,rgba(43,232,176,.07),transparent 70%),
      radial-gradient(52vw 42vh at 92% 8%,rgba(69,210,255,.055),transparent 70%);
  }
  main{position:relative;width:min(760px,100%)}
  h1{
    font:640 15px/1 var(--sans);letter-spacing:.08em;margin:0 0 6px;
    background:linear-gradient(92deg,var(--neon),var(--cyan));
    -webkit-background-clip:text;background-clip:text;color:transparent;
  }
  .sous{color:var(--pale);font-size:13px;margin-bottom:26px}

  .etape{
    background:linear-gradient(180deg,rgba(14,26,33,.72),rgba(10,18,24,.9));
    border:1px solid var(--trait);border-radius:14px;padding:20px 22px;
    margin-bottom:14px;box-shadow:0 22px 54px -32px rgba(0,0,0,.95);
  }
  .etape[aria-disabled="true"]{opacity:.4;pointer-events:none}
  .titre{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .n{
    width:22px;height:22px;border-radius:50%;flex:0 0 auto;
    display:grid;place-items:center;font:640 11px var(--mono);
    border:1px solid var(--trait2);color:var(--pale);
  }
  .etape.faite .n{background:var(--neon);border-color:var(--neon);color:#052018}
  h2{font:640 11px/1 var(--sans);margin:0;text-transform:uppercase;letter-spacing:.13em;color:var(--doux)}
  .aide{color:var(--pale);font-size:12.5px;margin:0 0 16px 32px}

  label{display:block;font-size:12.5px;color:var(--doux);margin:12px 0 5px}
  input[type=text],input[type=password]{
    width:100%;padding:9px 11px;border-radius:9px;background:rgba(4,7,10,.6);
    border:1px solid var(--trait2);color:var(--encre);font:13px var(--mono);
  }
  input:focus{outline:none;border-color:var(--neon);box-shadow:0 0 0 3px rgba(43,232,176,.12)}
  .duo{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  @media (max-width:620px){.duo{grid-template-columns:1fr}}

  button{
    font:640 11px/1 var(--sans);letter-spacing:.07em;text-transform:uppercase;
    padding:10px 16px;border-radius:9px;cursor:pointer;margin-top:18px;
    border:1px solid var(--neon);background:rgba(43,232,176,.12);color:var(--neon);
    transition:background .18s,box-shadow .18s;
  }
  button:hover{background:rgba(43,232,176,.2);box-shadow:0 0 18px rgba(43,232,176,.2)}
  button[disabled]{opacity:.45;cursor:default;box-shadow:none}

  .depots{display:grid;gap:6px;max-height:320px;overflow-y:auto;margin-top:6px}
  .depot{
    display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;
    padding:9px 11px;border:1px solid var(--trait);border-radius:10px;
    background:rgba(8,14,19,.5);
  }
  .depot .nom{font:600 13px var(--mono)}
  .depot .lang{color:var(--pale);font-size:11.5px;margin-left:8px}
  .choix{display:flex;gap:4px}
  .choix button{
    margin:0;padding:5px 9px;font-size:10px;border-color:var(--trait2);
    background:transparent;color:var(--pale);
  }
  .choix button[aria-pressed="true"]{border-color:var(--neon);color:var(--neon);background:rgba(43,232,176,.12)}
  .choix button.ctx[aria-pressed="true"]{border-color:var(--cyan);color:var(--cyan);background:rgba(69,210,255,.12)}

  .fil{margin-top:14px;max-height:340px;overflow-y:auto;font:12.5px var(--mono)}
  .l{display:grid;grid-template-columns:78px 1fr;gap:10px;padding:4px 0}
  .l .q{font:640 10px var(--sans);text-transform:uppercase;letter-spacing:.08em;color:var(--pale)}
  .l.ok .q{color:var(--neon)} .l.erreur .q,.l.erreur .t{color:var(--rouge)}
  .l.etape2 .q{color:var(--cyan)} .l.termine .q{color:var(--neon)}
  .avis{
    margin-top:14px;padding:10px 12px;border-radius:9px;font-size:12.5px;
    border:1px solid var(--ambre);color:var(--ambre);background:rgba(255,192,97,.07);
  }
  .erreur-boite{border-color:var(--rouge);color:var(--rouge);background:rgba(255,110,126,.07)}
</style>
</head>
<body>
<main>
  <h1>REVIEWER</h1>
  <div class="sous">Installation &mdash; ce demon n'a pas encore de configuration.</div>

  <section class="etape" id="e1">
    <div class="titre"><span class="n">1</span><h2>La forge</h2></div>
    <p class="aide">Le jeton est verifie contre GitHub avant tout le reste : une
      configuration qui reference un jeton mort a l'air juste et echoue plus tard,
      dans un message qui parle d'autre chose.</p>
    <label>Organisation ou compte GitHub</label>
    <input type="text" id="org" placeholder="mon-org" autocomplete="off">
    <div class="duo">
      <div>
        <label>Jeton de LECTURE &mdash; requis</label>
        <input type="password" id="tr" autocomplete="off">
      </div>
      <div>
        <label>Jeton d'ECRITURE &mdash; facultatif</label>
        <input type="password" id="tw" autocomplete="off">
      </div>
    </div>
    <p class="aide" style="margin:10px 0 0 0">Sans jeton d'ecriture, l'agent corrige
      dans son worktree sans rien pousser ni repondre. C'est un bon premier cran.</p>
    <button id="b1">Voir mes depots</button>
    <div id="err1"></div>
  </section>

  <section class="etape" id="e2" aria-disabled="true">
    <div class="titre"><span class="n">2</span><h2>Les depots</h2></div>
    <p class="aide"><b>write</b> : l'agent y corrige et y pousse.
       <b>context</b> : il le lit &mdash; code, conventions &mdash; sans pouvoir l'ecrire.
       Non coche : ignore.</p>
    <div class="depots" id="depots"></div>

    <div class="duo">
      <div>
        <label>Relecteurs de confiance, separes par des virgules</label>
        <input type="text" id="relecteurs" placeholder="chatgpt-codex-connector[bot], moi">
      </div>
      <div>
        <label>Qui prevenir</label>
        <input type="text" id="notify" placeholder="@moi">
      </div>
    </div>
    <p class="aide" style="margin:10px 0 0 0">Seuls les relecteurs de cette liste
      peuvent declencher un cycle. Elle vient d'ici, JAMAIS de la charge utile :
      c'est elle qui empeche un commentaire quelconque de consommer du quota.</p>

    <div class="duo">
      <div>
        <label>Jeton OAuth Claude</label>
        <input type="password" id="oauth" autocomplete="off">
      </div>
      <div>
        <label>Ne traiter que les PR de (vide = toutes)</label>
        <input type="text" id="auteurs" placeholder="moi">
      </div>
    </div>
    <p class="aide" style="margin:10px 0 0 0">Si plusieurs personnes lancent chacune
      leur demon sur les memes depots, restreindre par auteur les empeche de se
      marcher dessus : les baux sont locaux, il n'y a aucune exclusion entre machines.</p>

    <button id="b2">Installer</button>
    <div id="err2"></div>
  </section>

  <section class="etape" id="e3" aria-disabled="true">
    <div class="titre"><span class="n">3</span><h2>Installation</h2></div>
    <p class="aide">Clonage des depots, installation de leurs dependances, ecriture
      de la configuration. Les dependances comptent : sans elles les verifications
      echouent, et le demon refuse de commiter du code pourtant bon.</p>
    <div class="fil" id="fil"></div>
  </section>
</main>

<script>
const $ = (s) => document.querySelector(s);
let DEPOTS = [], ETAT = {};

function avis(ou, texte, mauvais) {
  $(ou).innerHTML = texte
    ? `<div class="avis ${mauvais ? "erreur-boite" : ""}">${texte}</div>` : "";
}

async function poster(url, corps) {
  const r = await fetch(url, {method: "POST",
    headers: {"content-type": "application/json"}, body: JSON.stringify(corps)});
  const t = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(t.detail || `${r.status}`);
  return t;
}

$("#b1").onclick = async () => {
  const org = $("#org").value.trim(), tr = $("#tr").value.trim();
  if (!org || !tr) return avis("#err1", "L'organisation et le jeton de lecture sont requis.", true);
  $("#b1").disabled = true; avis("#err1", "Verification du jeton\\u2026");
  try {
    const d = await poster("/api/depots", {org, token_read: tr, token_write: $("#tw").value.trim()});
    DEPOTS = d.depots;
    avis("#err1", `Jeton valide \\u2014 compte « ${d.login} ». ${DEPOTS.length} depot(s) visible(s).`);
    $("#e1").classList.add("faite");
    $("#e2").setAttribute("aria-disabled", "false");
    $("#auteurs").value = d.login;
    dessinerDepots();
    $("#e2").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (e) { avis("#err1", String(e.message), true); }
  $("#b1").disabled = false;
};

function dessinerDepots() {
  const b = $("#depots"); b.textContent = "";
  for (const d of DEPOTS) {
    const l = document.createElement("div");
    l.className = "depot";
    l.innerHTML = `<div><span class="nom">${d.nom}</span>`
      + `<span class="lang">${d.langage || ""}${d.prive ? " · prive" : ""}</span></div>`
      + `<div class="choix">`
      + `<button data-a="write" aria-pressed="false">write</button>`
      + `<button class="ctx" data-a="context" aria-pressed="false">context</button></div>`;
    for (const bt of l.querySelectorAll("button")) {
      bt.onclick = () => {
        const a = bt.dataset.a;
        ETAT[d.nom] = ETAT[d.nom] === a ? null : a;
        for (const x of l.querySelectorAll("button"))
          x.setAttribute("aria-pressed", String(ETAT[d.nom] === x.dataset.a));
      };
    }
    b.appendChild(l);
  }
}

$("#b2").onclick = async () => {
  const depots = Object.entries(ETAT).filter(([, a]) => a)
    .map(([nom, access]) => ({nom, access}));
  if (!depots.length) return avis("#err2", "Cocher au moins un depot.", true);
  const liste = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
  $("#b2").disabled = true; avis("#err2", "");
  try {
    await poster("/api/installer", {
      org: $("#org").value.trim(),
      projet: $("#org").value.trim().toLowerCase(),
      token_read: $("#tr").value.trim(), token_write: $("#tw").value.trim(),
      oauth: $("#oauth").value.trim(), depots,
      relecteurs: liste($("#relecteurs").value), notify: $("#notify").value.trim(),
      auteurs: liste($("#auteurs").value),
    });
    $("#e2").classList.add("faite");
    $("#e3").setAttribute("aria-disabled", "false");
    $("#e3").scrollIntoView({behavior: "smooth", block: "start"});
    suivre();
  } catch (e) { avis("#err2", String(e.message), true); $("#b2").disabled = false; }
};

function suivre() {
  const flux = new EventSource("/api/progres"), fil = $("#fil");
  flux.onmessage = (m) => {
    let e; try { e = JSON.parse(m.data); } catch { return; }
    if (e.genre === "fin") {
      flux.close();
      // Le demon relit sa configuration au demarrage : c'est lui qui doit
      // repartir, pas la page qui doit faire semblant.
      fil.insertAdjacentHTML("beforeend",
        '<div class="l termine"><span class="q">suite</span>'
        + '<span class="t">Relancer le conteneur : docker compose restart</span></div>');
      return;
    }
    const cls = {ok: "ok", erreur: "erreur", etape: "etape2", termine: "termine"}[e.genre] || "";
    fil.insertAdjacentHTML("beforeend",
      `<div class="l ${cls}"><span class="q">${e.genre}</span>`
      + `<span class="t">${e.texte}</span></div>`);
    fil.scrollTop = fil.scrollHeight;
  };
}
</script>
</body>
</html>
"""
