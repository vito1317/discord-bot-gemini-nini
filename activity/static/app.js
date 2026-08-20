/* 奈奈操作台 —— Discord Activity 前端
 *
 * 兩件事最容易踩雷，所以寫在最前面：
 *
 * 1. **路徑要走 /.proxy/** —— Activity 跑在 <app_id>.discordsays.com 的 iframe 裡，
 *    所有對外請求都得經過 Discord 的代理。後端兩種路徑都有掛，但這裡一律用
 *    /.proxy/api/... 才不會因為環境不同而失敗。
 *
 * 2. **身分一定要後端驗** —— 這個面板按一下就會送出真的掛號。所以前端只負責
 *    把 OAuth code 交出去，是誰、能不能操作全部由後端判斷（見 activity.py）。
 */

const API = "/.proxy/api";
const $ = (id) => document.getElementById(id);

let token = null;
let me = null;
let state = null;
let manual = false;              // 人是不是接手了
let soundOn = false;             // 瀏覽器的聲音有沒有接進語音頻道
let viewport = [1280, 900];      // 瀏覽器實際的 viewport（換算點擊座標用）

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: "Bearer " + token } : {}),
      ...(opts.headers || {}),
    },
  });
  if (!res.ok) {
    let msg = res.status === 403 ? "這是別人的任務，你不能操作"
            : res.status === 404 ? "現在沒有進行中的任務"
            : "出錯了（" + res.status + "）";

    // 要先看得出「這個回應到底是誰給的」。後端一律回 JSON；如果回來的是 HTML，
    // 那就是中間的閘道（WAF／反向代理）把請求擋掉了，跟權限無關。
    // 之前沒分這一層，WAF 擋掉 POST /api/manual 回它自己的 403 攔截頁，前端照
    // 403 的預設字串顯示「這是別人的任務，你不能操作」—— 把攔截誤導成權限問題，
    // 害人往「身分驗證壞了」的方向查。
    let body = "";
    try { body = await res.text(); } catch (_) {}
    let j = null;
    try { j = JSON.parse(body); } catch (_) {}

    if (j && j.error) {
      msg = j.error;
    } else if (body && /<html|<!doctype/i.test(body)) {
      msg = "請求被中間的閘道擋掉了（" + res.status + "）—— 不是奈奈拒絕你";
      window.__nlog("gateway-block",
                    res.status + " " + path + " │ " + body.slice(0, 200), true);
    }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

/* ── 登入：SDK 拿 code → 後端換身分 ── */
async function login() {
  // client_id 一定要用伺服器嵌進來的那個 —— Discord 的 iframe 網址**不會**帶
  // client_id（它帶的是 frame_id / instance_id / guild_id / channel_id），
  // 之前從 query 讀會拿到 undefined，SDK 的 handshake 就卡在 ready() 不回來，
  // 前端一直停在「連線中…」，所有 API 呼叫都回 401。
  const clientId = window.__CLIENT_ID__ || new URLSearchParams(location.search).get("client_id");
  if (!clientId || clientId.indexOf("__") === 0) {
    throw new Error("伺服器沒有提供 client_id");
  }

  window.__nlog("login-begin", "clientId=" + clientId);
  if (typeof window.DiscordSDK !== "function") {
    throw new Error("Discord SDK 沒載進來（static/sdk.js 被擋或沒抓到）");
  }

  const sdk = new window.DiscordSDK(clientId);
  // ready() 是跟 Discord 母視窗的 handshake。它不會 timeout —— 卡住的話這裡
  // 就永遠不回來，前端停在「連線中…」什麼都不做（之前 client_id 抓錯就是這樣，
  // 從外面看只是一片黑）。所以自己加時限，卡住要講出來。
  await Promise.race([
    sdk.ready(),
    new Promise((_, rej) => setTimeout(
      () => rej(new Error("跟 Discord 的 handshake 超過 15 秒沒回應")), 15000)),
  ]);
  window.__nlog("sdk-ready", "ok");

  // **不要**在這裡帶 redirect_uri。這一段來回踩了兩次，兩個錯誤看起來互相矛盾：
  //
  //   不帶 → OAuth2 Error: invalid_request: Missing "redirect_uri" in request.
  //   帶了 → Redirect URI cannot be used in the RPC OAuth2 Authorization flow
  //
  // 真正的規則是：RPC（SDK）的 authorize 會自己去用 App 上**已註冊**的
  // redirect URI，所以參數不能傳；而前面那個 "Missing" 其實是在說
  // 「這個 App 一個 redirect URI 都沒註冊」，不是在要你把它塞進請求裡。
  //
  // 換句話說解法在 Developer Portal，不在這段程式：
  //   Developer Portal → OAuth2 → Redirects 要有一筆（目前註冊的是
  //   https://discord.vito1317.com，用 GET /applications/@me 的 redirect_uris 可查）。
  // 那一筆被刪掉的話，這裡就會退回 "Missing redirect_uri" —— 別再往這裡加參數。
  window.__nlog("authorize-begin", "scope=identify（不帶 redirect_uri）");
  const { code } = await sdk.commands.authorize({
    client_id: clientId,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify"],
  });

  const out = await api("/auth", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  token = out.token;
  me = out.user;
  $("meta").textContent = "你是 " + me.name;
  $("dot").classList.add("on");
  window.__nlog("auth-ok", me.name);
}

/* ── 畫面：一直重抓 /api/frame 當「慢速直播」 ── */
function showFrame(src) {
  if (streaming) return;   // 串流正在餵這個 <img>，別讓遲到的單張蓋掉它
  const img = $("img");
  const old = img.src;
  img.src = src;
  img.style.display = "block";
  $("empty").style.display = "none";
  if (old.startsWith("blob:")) URL.revokeObjectURL(old);
}

function showEmpty(text) {
  const img = $("img");
  if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
  img.removeAttribute("src");
  img.style.display = "none";
  $("empty").textContent = text;
  $("empty").style.display = "block";
}

/* ── 連續畫面：一條長連線推 JPEG（MJPEG）──
 *
 * 一張一張抓的話每張都要繞 Discord 的代理一趟，那條路實測有好幾秒的離群值，
 * 所以最多只能到 1 fps 左右 —— 影片是幻燈片。改成 <img> 直接吃一條 multipart
 * 串流，代理只走一次，實際可以到 8 fps 左右。
 *
 * 但**退路一定要留**：Discord 的代理會不會乖乖轉 multipart、會不會緩衝，
 * 我沒辦法從伺服器端測。所以串流一失敗就自動回去用原本的輪詢，
 * 至少還看得到畫面，而不是變成一片黑。
 */
let streaming = false;
let streamFellBack = false;
let streamTries = 0;
const STREAM_MAX_TRIES = 2;

async function startStream() {
  if (streaming || streamFellBack || !token) return;
  if (++streamTries > STREAM_MAX_TRIES) {
    streamFellBack = true;
    window.__nlog("stream-give-up",
                  "連續 " + STREAM_MAX_TRIES + " 次都撐不住，改用逐張輪詢", true);
    pullFrame();
    return;
  }
  streaming = true;                       // 先佔位，避免同時被叫兩次

  // <img> 沒辦法帶 Authorization header，所以憑證只能放在網址上，而網址會進
  // nginx 的 access log —— 因此這裡換的是一張「只能看畫面、2 分鐘就過期」的票，
  // 不是那個 6 小時什麼都能做的 token。
  let ticket;
  try {
    ticket = (await api("/ticket")).ticket;
  } catch (e) {
    streaming = false;
    window.__nlog("stream-ticket-fail", e.message, true);
    return;
  }

  const img = $("img");
  img.onerror = () => {
    if (!streaming) return;
    streaming = false;
    streamFellBack = true;                // 只退一次，不要一直重試
    window.__nlog("stream-fallback", "串流不通，退回逐張輪詢", true);
    img.onerror = null;
    pullFrame();
  };
  img.src = API + "/stream?ticket=" + encodeURIComponent(ticket) + "&t=" + Date.now();
  img.style.display = "block";
  $("empty").style.display = "none";
  window.__nlog("stream-start", "開始連續畫面（第 " + streamTries + " 次）");
  // 撐過 30 秒就算這條路走得通 —— 偶發斷線不該累積成永久降級
  setTimeout(() => { if (streaming) streamTries = 0; }, 30000);
}

function stopStream() {
  if (!streaming) return;
  streaming = false;
  const img = $("img");
  img.onerror = null;
  img.removeAttribute("src");
}

let frameBusy = false;
async function pullFrame() {
  if (streaming) return;      // 串流在跑就不用逐張抓了
  if (frameBusy || !token) return;
  frameBusy = true;

  // 這個時限是必要的，不是保險。frameBusy 是一個門閂，只要有一次抓取永遠不
  // settle（代理把連線掛住、回應中途斷掉、換頁時 Discord 的 proxy 卡住都會），
  // 門閂就永遠關著 —— 之後每一輪 pullFrame 都在第一行 return，畫面停在最後一張
  // 截圖再也不動，而 /api/state 照樣每 2 秒更新。
  // 實測正是如此：nginx log 顯示 /api/frame 在某一刻之後一筆都沒再送出，
  // /api/state 卻連跑了三分鐘 —— 使用者看到的就是「畫面卡住，但她好像還在動」。
  const ctl = new AbortController();
  const bell = setTimeout(() => ctl.abort(), 5000);
  try {
    const res = await fetch(API + "/frame?t=" + Date.now(), {
      headers: { Authorization: "Bearer " + token },
      signal: ctl.signal,
    });
    if (res.ok) {
      showFrame(URL.createObjectURL(await res.blob()));
    } else if (res.status === 404) {
      // 任務做完 session 就收掉了，這裡開始回 404。原本什麼都不做 → 畫面停在
      // 最後一張截圖不動，看起來跟「壞掉卡住」一模一樣（使用者就是這樣回報的）。
      // 停了要講清楚是停了。
      showEmpty("任務已經結束了，沒有畫面囉");
    }
  } catch (e) {
    /* 換頁中抓不到是正常的，下一輪再說；但被時限砍掉要留紀錄 */
    if (e && e.name === "AbortError") window.__nlog("frame-timeout", "5 秒沒回應", true);
  } finally {
    clearTimeout(bell);
    frameBusy = false;
  }
}

/* ── 狀態 ── */
function renderSteps(steps) {
  const box = $("steps");
  if (!steps || !steps.length) {
    box.innerHTML = '<div style="color:var(--dim)">還沒有步驟</div>';
    return;
  }
  box.innerHTML = steps
    .map((s) => `<div><b>${s.n}.</b> ${escapeHtml(s.detail)}</div>`)
    .join("");
  box.scrollTop = box.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function apply(s) {
  state = s;
  manual = !!s.paused;
  const linger = !!s.lingering;      // 任務做完了，畫面留著給人自己用
  if (s.viewport) viewport = s.viewport;
  // 留著的時候沒有「交還」這回事 —— 任務已經結束，她不會再接手做下去了，
  // 顯示「交還給奈奈」只會讓人以為按了她就會繼續。
  $("bManual").textContent = linger ? "🖐 這一頁交給你了"
                                    : (manual ? "🤖 交還給奈奈" : "🖐 我來操作");
  $("bManual").disabled = linger;

  soundOn = !!s.audio_on;
  $("bSound").textContent = soundOn ? "🔇 關掉聲音" : "🔊 接到語音";
  // 瀏覽器本身沒聲音（音效環境起不來）就不要給一顆按了沒用的鈕
  $("bSound").disabled = !s.audio_capable;
  $("bSound").title = s.audio_capable
    ? "把瀏覽器的聲音接進你所在的語音頻道"
    : "這個瀏覽器沒有聲音（伺服器的音效環境沒起來）";
  $("manualHint").style.display = manual ? "block" : "none";
  $("manualHint").innerHTML = linger
    ? "<b>任務做完了，畫面留給你</b> —— 點左邊畫面就是點網頁、滾輪可以捲動、"
      + "下面可以打字。<br>把面板關掉之後就會自動收起來。"
    : "<b>你現在在直接操作那個瀏覽器</b> —— 點左邊畫面就是點網頁、滾輪可以捲動。"
      + "奈奈已經停下來等你。驗證碼、登入這種她過不去的，你自己來最快。";
  $("typeRow").style.display = manual ? "flex" : "none";
  $("typeHint").style.display = manual ? "block" : "none";
  $("img").style.cursor = manual ? "crosshair" : "default";
  const watchers = s.watchers > 1 ? `　👀 ${s.watchers} 人在看` : "";
  $("meta").textContent = (me ? "你是 " + me.name : "") + watchers;

  if (!s.active) {
    $("task").textContent = s.hint || "沒有進行中的任務";
    $("badge").style.display = "none";
    $("ask").style.display = "none";
    // 狀態說沒任務，畫面就不該還掛著上一個任務的截圖（會被當成當前畫面）
    stopStream();
    showEmpty("沒有進行中的任務 —— 在右邊交代一件事給她");
    renderSteps([]);
    setButtons({ yes: false, no: false, always: false, stop: false, send: false });
    return;
  }

  $("task").textContent = s.task;
  $("badge").style.display = "block";
  $("badge").textContent = (linger ? "✅ 做完了，畫面留著"
                                   : (s.running ? "● 進行中" : "⏸ 等你")) +
    (s.title ? "　" + s.title : "");

  // 連續畫面的看門狗。
  //
  // **一定要用伺服器那邊的數字當真相。** <img> 吃 multipart 串流被中途掐斷時，
  // onerror 不保證會觸發 —— 手機版實測就是不觸發：伺服器 1 秒後就看到連線斷了
  // （log 寫「推了 7 幀」），前端卻還以為在播，於是 pullFrame 一直早退，
  // 畫面就永遠停在最後那一幀（使用者看到的「卡在搜尋階段」）。
  if (streaming && s.streaming === false) {
    streaming = false;
    $("img").onerror = null;
    window.__nlog("stream-died", "伺服器說連線已斷，重接（第 " + streamTries + " 次）", true);
  }
  if (!streaming && !streamFellBack) startStream();

  renderSteps(s.steps);

  const canAct = s.you_can_act;
  const askConfirm = s.awaiting === "confirm";
  const askInput = s.awaiting === "input";

  const ask = $("ask");
  if (askConfirm) {
    ask.style.display = "block";
    ask.innerHTML = "✋ <b>她準備送出了</b>，這一步送出去就不能反悔 —— 要按下去嗎？";
  } else if (askInput) {
    ask.style.display = "block";
    ask.innerHTML = "❓ <b>她需要你給資料</b>（例如身分證、生日、圖形驗證碼）—— " +
      "看左邊的畫面，打在下面的欄位。";
  } else {
    ask.style.display = "none";
  }

  setButtons({
    yes: canAct && askConfirm,
    always: canAct && askConfirm,
    no: canAct && askConfirm,
    // 留著的畫面也要能主動收起來（⏹ 這時候等於「我看完了，收掉吧」）
    stop: canAct && (s.running || !!s.awaiting || linger),
    send: canAct && askInput,
  });
  $("bStop").textContent = linger ? "🚪 收起來" : "⏹ 停止";

  if (!canAct) {
    $("task").textContent += "（這是別人交代的任務，你只能看）";
  }
}

function setButtons(o) {
  $("bYes").disabled = !o.yes;
  $("bAlways").disabled = !o.always;
  $("bNo").disabled = !o.no;
  $("bStop").disabled = !o.stop;
  $("bSend").disabled = !o.send;
}

async function pullState() {
  if (!token) return;
  try {
    apply(await api("/state"));
  } catch (e) {
    $("meta").textContent = e.message;
  }
}

/* ── 按鈕 ── */
function wire() {
  const act = async (fn) => {
    try {
      const r = await fn();
      if (r && r.msg) toast(r.msg);
      // 後端把操作後的新畫面跟著回應一起帶回來了 —— 直接畫上去。
      // 等下一輪 pullFrame 的話至少慢 1.2 秒，而且要多繞 Discord 的代理一趟
      // （那一趟實測會出現好幾秒的離群值），操作起來就是一直在等。
      if (r && r.frame) showFrame("data:image/png;base64," + r.frame);
      await pullState();
    } catch (e) {
      toast(e.message);
    }
  };

  $("bYes").onclick = () => act(() =>
    api("/confirm", { method: "POST", body: JSON.stringify({ allow: true }) }));

  $("bAlways").onclick = () => act(() =>
    api("/confirm", { method: "POST",
                      body: JSON.stringify({ allow: true, always: true }) }));

  $("bNo").onclick = () => act(() =>
    api("/confirm", { method: "POST", body: JSON.stringify({ allow: false }) }));

  $("bStop").onclick = () => act(() => api("/stop", { method: "POST" }));

  const send = () => {
    const f = $("fInput");
    const text = f.value.trim();
    if (!text) return;
    f.value = "";
    act(() => api("/input", { method: "POST", body: JSON.stringify({ text }) }));
  };
  $("bSend").onclick = send;
  $("fInput").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });

  const newTask = () => {
    const f = $("fTask");
    const task = f.value.trim();
    if (!task) return;
    f.value = "";
    act(() => api("/task", { method: "POST", body: JSON.stringify({ task }) }));
  };
  // ── 人接手直接操作 ──
  $("bManual").onclick = () => act(() =>
    api("/manual", { method: "POST", body: JSON.stringify({ on: !manual }) }));

  // 聲音：把瀏覽器的音訊接進語音頻道（她的說話聲會疊在一起，不會互相蓋掉）
  $("bSound").onclick = () => act(() =>
    api("/audio", { method: "POST", body: JSON.stringify({ on: !soundOn }) }));

  // 點畫面 → 換算成瀏覽器的 viewport 座標。
  // 圖是等比縮放後畫出來的，所以要用 img 實際被畫出來的矩形來換算，
  // 不能直接用 offsetX（那是相對於 element 的 padding box）。
  $("img").addEventListener("click", (e) => {
    if (!manual) { toast("先按「我來操作」"); return; }
    const r = e.target.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const x = ((e.clientX - r.left) / r.width) * viewport[0];
    const y = ((e.clientY - r.top) / r.height) * viewport[1];
    act(() => api("/click", { method: "POST",
                              body: JSON.stringify({ x: Math.round(x), y: Math.round(y) }) }));
  });

  $("img").addEventListener("wheel", (e) => {
    if (!manual) return;
    e.preventDefault();
    act(() => api("/scroll", { method: "POST",
                               body: JSON.stringify({ dy: e.deltaY > 0 ? 400 : -400 }) }));
  }, { passive: false });

  const sendType = (enter) => {
    const f = $("fType");
    const text = f.value;
    if (!text && !enter) return;
    f.value = "";
    act(() => api("/type", { method: "POST",
                             body: JSON.stringify({ text, enter }) }));
  };
  $("fType").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sendType(true); }
  });

  // 按鍵鈕。**⏎ 一定要連框裡的字一起送** ——
  // 後端的 /api/type 是「有 key 就只送那個鍵、忽略 text」，所以先前 ⏎ 只送了
  // 一個 Enter，打在框裡的字整段被丟掉、框也沒清空，看起來就是「打字沒反應」。
  // 這是最容易按的那顆鈕，行為必須是人以為的那個。
  const key = (k) => act(() => api("/type", { method: "POST",
                                              body: JSON.stringify({ key: k }) }));
  $("bKeyEnter").onclick = () => {
    if ($("fType").value) sendType(true);   // 有字 → 打完字再按 Enter
    else key("Enter");                      // 沒字 → 單純送一個 Enter
  };
  $("bKeyTab").onclick = () => key("Tab");
  $("bKeyBack").onclick = () => key("Backspace");

  $("bTask").onclick = newTask;
  $("fTask").addEventListener("keydown", (e) => { if (e.key === "Enter") newTask(); });
}

/* ── 啟動 ── */
(async () => {
  wire();
  try {
    await login();
  } catch (e) {
    window.__nlog("login-fail", e.message, true);
    $("meta").textContent = "授權失敗：" + e.message;
    $("task").textContent =
      "沒辦法確認你的身分，所以不能操作（" + e.message + "）。" +
      "請從 Discord 語音頻道的「活動」進來。";
    return;
  }
  await pullState();
  setInterval(pullState, 2000);
  // 逐張輪詢只是退路（串流不通時 startStream 會把 streamFellBack 打開）。
  // pullFrame 自己第一行就會在串流跑著的時候直接 return。
  setInterval(pullFrame, 1200);
})();
