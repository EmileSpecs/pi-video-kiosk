const grid = document.getElementById("grid");
const locked = document.getElementById("locked");
let busy = false;

function prevent(event) { event.preventDefault(); }
document.addEventListener("contextmenu", prevent);
document.addEventListener("selectstart", prevent);

async function load() {
  const response = await fetch("/api/catalog");
  const items = await response.json();
  grid.replaceChildren();
  for (const item of items) {
    const button = document.createElement("button");
    button.className = "tile";
    button.type = "button";
    if (item.thumb) button.style.backgroundImage = `url("${item.thumb}")`;
    button.addEventListener("click", () => play(item.name, button));
    grid.appendChild(button);
  }
}

async function play(name, button) {
  if (busy) return;
  busy = true;
  button.animate([{ transform: "scale(0.96)" }, { transform: "scale(1)" }], { duration: 300 });
  await fetch("/api/play", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  });
  setTimeout(() => { busy = false; }, 2000);
}

function apply(state) {
  locked.hidden = !state.ui_locked;
  for (const button of grid.querySelectorAll("button")) {
    button.classList.toggle("now", false);
  }
}

function connect() {
  const socket = new WebSocket(`ws://${location.host}/ws`);
  socket.onmessage = (event) => apply(JSON.parse(event.data));
  socket.onclose = () => setTimeout(connect, 2000);
  setInterval(() => { if (socket.readyState === 1) socket.send("heartbeat"); }, 30000);
}

load().catch(() => setTimeout(load, 2000));
connect();
