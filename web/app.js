"use strict";
const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="orchestrator-token"]').content;
const labels = { queued: "W kolejce", running: "W toku", succeeded: "Ukończone", failed: "Błąd", cancelled: "Anulowane", interrupted: "Przerwane" };
const state = { projects: [], jobs: [], selected: null, current: null, lastEvent: 0, listKey: "", refreshing: false, user: null };
let toastTimer;

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options, headers: { "X-Orchestrator-Token": token, "Content-Type": "application/json", ...options.headers }
  });
  if (!response.ok) {
    if (response.status === 401) { window.location.assign("/login"); throw new Error("Sesja wygasła."); }
    const payload = await response.json().catch(() => ({}));
    const detail = Array.isArray(payload.detail) ? payload.detail.map((e) => e.msg).join("; ") : payload.detail;
    throw new Error(detail || `Błąd połączenia (${response.status})`);
  }
  return response;
}
async function json(path, options) { return (await api(path, options)).json(); }
function post(path, body) { return json(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }); }
function text(selector, value) { $(selector).textContent = value ?? ""; }
function element(tag, className, value) { const node = document.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = value; return node; }
function badge(status) { return element("span", `badge ${status}`, labels[status] || status); }
function date(value) { return value ? new Date(value).toLocaleString("pl-PL", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : ""; }
function toast(message) { text("#toast", message); $("#toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4500); }
function showPage(page) {
  document.querySelectorAll(".page").forEach((node) => { node.hidden = node.id !== `page-${page}`; });
  document.querySelectorAll(".nav-button").forEach((node) => node.classList.toggle("active", node.dataset.page === page));
  text("#breadcrumb", { tasks: "Zadania", projects: "Projekty", system: "Połączenia" }[page]);
}
function selectOptions(selector, rows, emptyLabel) {
  const select = $(selector), previous = select.value;
  select.replaceChildren();
  if (emptyLabel) select.add(new Option(emptyLabel, ""));
  rows.forEach((row) => select.add(new Option(row.name, row.id)));
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
}
async function loadProjects() {
  state.projects = await json("/projects");
  selectOptions("#project-filter", state.projects, "Wszystkie projekty");
  selectOptions("#task-project", state.projects);
  text("#metric-projects", state.projects.length);
  $("#empty-projects").hidden = state.projects.length > 0;
  const cards = $("#project-cards"); cards.replaceChildren();
  for (const project of state.projects) {
    const card = element("article", "panel project-card");
    card.append(element("span", "metric-icon violet", "▱"), element("h2", "", project.name), element("code", "", project.remote_url || project.path));
    if (project.branch) card.append(element("p", "", `Gałąź: ${project.branch}`));
    card.append(element("p", "", project.instructions || "Bez dodatkowych instrukcji dla agenta."));
    const button = element("button", "secondary", "＋ Nowe zadanie");
    button.addEventListener("click", () => openTask(project.id)); card.append(button); cards.append(card);
  }
  $("#empty-add-project").textContent = state.projects.length ? "＋ Nowe zadanie" : "＋ Dodaj projekt";
}
function renderJobs() {
  const filter = $("#project-filter").value, query = $("#search").value.toLocaleLowerCase("pl");
  const jobs = state.jobs.filter((job) => (!filter || job.project_id === filter) && `${job.title} ${job.project_name}`.toLocaleLowerCase("pl").includes(query));
  const active = state.jobs.filter((job) => ["queued", "running"].includes(job.status)).length;
  text("#metric-active", active); text("#queue-count", active); text("#metric-done", state.jobs.filter((job) => job.status === "succeeded").length);
  text("#task-count", `${jobs.length} zadań`); $("#empty-list").hidden = jobs.length > 0;
  const key = JSON.stringify([jobs, state.selected]);
  if (key === state.listKey) return;
  state.listKey = key; const list = $("#task-list"); list.replaceChildren();
  for (const job of jobs) {
    const button = element("button", `task-row${job.id === state.selected ? " selected" : ""}`);
    button.setAttribute("aria-pressed", String(job.id === state.selected));
    const top = element("span", "task-row-top"); top.append(element("span", "task-row-title", job.title), badge(job.status));
    const info = element("span", "task-row-info");
    info.append(element("span", "", job.project_name), element("span", "", "·"), element("span", "", date(job.created_at)), element("span", "", job.provider === "claude" ? "Claude Code" : "Codex"));
    button.append(top, info); button.addEventListener("click", () => selectJob(job.id)); list.append(button);
  }
}
async function selectJob(id) {
  state.selected = id; state.current = null; state.lastEvent = 0;
  $("#event-log").replaceChildren(); renderJobs(); showPage("tasks");
  $("#job-detail").hidden = true; $("#empty-detail").hidden = false;
  await refreshDetail().catch(showError);
}
function renderDetail(job) {
  $("#empty-detail").hidden = true; $("#job-detail").hidden = false;
  text("#detail-project", job.project_name); text("#detail-title", job.title);
  const status = $("#detail-status"); status.className = `badge ${job.status}`; status.textContent = labels[job.status];
  const meta = [job.provider === "claude" ? "Claude Code" : "Codex", job.kind === "analysis" ? "Analiza" : "Zmiany w kodzie", date(job.created_at)];
  if (job.cost_usd != null) meta.push(`≈ $${job.cost_usd.toFixed(4)}`);
  if (job.output_tokens != null) meta.push(`${job.output_tokens.toLocaleString("pl")} tokenów wyjścia`);
  if (job.created_by) meta.push(`Zlecił(a): ${job.created_by}`);
  text("#job-meta", meta.join("  ·  "));
  const terminal = !["queued", "running"].includes(job.status);
  $("#retry-job").hidden = !terminal; $("#cancel-job").hidden = terminal || (state.user?.role !== "admin" && state.user?.username !== job.created_by);
  $("#cancel-job").disabled = Boolean(job.cancel_requested);
  text("#cancel-job", job.cancel_requested ? "Anulowanie…" : "Anuluj zadanie");
  $("#download-patch").hidden = !terminal || !job.diff;
  $("#job-error").hidden = !job.error; text("#job-error", job.error);
  text("#detail-prompt", job.prompt);
  text("#result-text", job.result || (job.status === "queued" ? "Zadanie czeka na wolnego agenta." : job.status === "running" ? "Agent pracuje. Otwórz zakładkę Przebieg, aby zobaczyć postępy." : "Agent nie zwrócił odpowiedzi. Szczegóły znajdziesz w przebiegu zadania."));
  $("#workspace-box").hidden = !job.workspace; text("#workspace-path", job.workspace); text("#base-commit", `Commit bazowy: ${job.base_commit.slice(0, 12)} · ${job.branch || ""}`);
  text("#diff-note", job.diff_truncated ? "Podgląd ograniczony do 2 MB. Pobierz patch, aby zobaczyć całość." : job.diff ? "Zmiany względem commita źródłowego, łącznie z nowymi plikami." : terminal ? "Brak zmian w plikach." : "Różnice pojawią się po zakończeniu pracy agenta.");
  if (!state.current || state.current.diff !== job.diff) {
    const diff = $("#diff-text"); diff.replaceChildren();
    // A bounded preview keeps large patches from freezing the panel.
    const lines = job.diff.split("\n");
    for (const line of lines.slice(0, 5000)) {
      const css = line.startsWith("+") ? "diff-added" : line.startsWith("-") ? "diff-removed" : line.startsWith("@@") || line.startsWith("diff ") ? "diff-heading" : "";
      diff.append(element("div", css, line || " "));
    }
    if (lines.length > 5000) diff.append(element("div", "muted", "Dalsza część zmian jest dostępna w pliku patch."));
  }
  state.current = job;
}
async function refreshDetail() {
  const id = state.selected; if (!id) return;
  const after = state.lastEvent;
  const [job, events] = await Promise.all([json(`/jobs/${id}`), json(`/jobs/${id}/events?after=${after}`)]);
  if (id !== state.selected) return;
  renderDetail(job);
  const log = $("#event-log");
  for (const event of events) {
    if (event.id <= state.lastEvent) continue;
    const row = element("div", `event ${event.kind}`);
    row.append(element("time", "", new Date(event.created_at).toLocaleTimeString("pl")), element("strong", "", { status: "STATUS", assistant: "AGENT", tool: "NARZĘDZIE", log: "LOG", error: "BŁĄD" }[event.kind] || event.kind), element("div", "", event.text));
    log.append(row); state.lastEvent = event.id;
    if (log.childElementCount > 1500) log.firstElementChild.remove();
  }
  if (events.length && $("#autoscroll").checked) log.scrollTop = log.scrollHeight;
}
function showError(error) { text("#banner", error.message); $("#banner").hidden = false; }
async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    state.jobs = await json("/jobs"); renderJobs(); await refreshDetail();
    $("#connection").classList.add("online"); $("#connection").lastChild.textContent = " Serwer · online";
    $("#banner").hidden = true;
  } catch (error) { $("#connection").classList.remove("online"); $("#connection").lastChild.textContent = " Brak połączenia"; showError(error); }
  finally { state.refreshing = false; }
}
async function loadHealth() {
  const health = await json("/health"); text("#data-path", health.data_dir);
  state.user = { username: health.username, role: health.role };
  $("#add-project").hidden = health.role !== "admin";
  $("#empty-add-project").hidden = !state.projects.length && health.role !== "admin";
  text("#username", health.username);
  const cards = $("#provider-cards"); cards.replaceChildren();
  for (const provider of [...health.providers, { name: "Git", available: health.git, path: "Repozytoria i przegląd zmian" }]) {
    const card = element("article", "panel project-card");
    card.append(element("span", `badge ${provider.available ? "succeeded" : "failed"}`, provider.available ? "CLI dostępne" : "Nie znaleziono"), element("h2", "", provider.name), element("code", "", provider.error || provider.path)); cards.append(card);
  }
  const jira = health.integrations.jira;
  const jiraCard = element("article", "panel project-card");
  jiraCard.append(element("span", `badge ${jira.configured ? "succeeded" : "queued"}`, jira.configured ? "Skonfigurowano" : "Do podłączenia"), element("h2", "", "Jira Comfortel"), element("code", "", jira.url || "Administrator ustawia adres i token na serwerze.")); cards.append(jiraCard);
  $("#import-jira").disabled = !jira.configured;
  text("#jira-hint", jira.configured ? "Pobierz opis, a następnie sprawdź projekt i zakres pracy." : "Import będzie dostępny po podłączeniu firmowej Jira.");
  for (const option of $("#task-provider").options) {
    const provider = health.providers.find((row) => row.id === option.value);
    option.disabled = !provider?.available;
  }
  if ($("#task-provider").selectedOptions[0]?.disabled) {
    const available = health.providers.find((row) => row.available);
    if (available) $("#task-provider").value = available.id;
  }
}
function openProject() { $("#project-form .form-error").hidden = true; $("#project-dialog").showModal(); }
function openTask(projectId) {
  if (!state.projects.length) { toast("Najpierw administrator musi dodać repozytorium projektu."); if (state.user?.role === "admin") openProject(); return; }
  $("#task-form .form-error").hidden = true;
  if (projectId) $("#task-project").value = projectId;
  $("#task-dialog").showModal();
}
function bindForm(selector, action) {
  $(selector).addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget, button = form.querySelector('[type="submit"]'), error = form.querySelector(".form-error");
    button.disabled = true; error.hidden = true;
    try { await action(Object.fromEntries(new FormData(form))); }
    catch (exc) { error.textContent = exc.message; error.hidden = false; }
    finally { button.disabled = false; }
  });
}
document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.close).close()));
document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-tab]").forEach((tab) => { const active = tab === button; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); });
  document.querySelectorAll(".tab-content").forEach((panel) => { panel.hidden = panel.id !== `view-${button.dataset.tab}`; });
  if (button.dataset.tab === "logs" && $("#autoscroll").checked) $("#event-log").scrollTop = $("#event-log").scrollHeight;
}));
$("#new-task").addEventListener("click", () => openTask()); $("#add-project").addEventListener("click", openProject);
$("#empty-add-project").addEventListener("click", () => state.projects.length ? openTask() : openProject());
$("#search").addEventListener("input", renderJobs); $("#project-filter").addEventListener("change", renderJobs);
$("#refresh-health").addEventListener("click", () => loadHealth().then(() => toast("Sprawdzono dostępność narzędzi.")).catch(showError));
$("#task-kind").addEventListener("change", () => text("#mode-hint", $("#task-kind").value === "analysis" ? "Agent przeanalizuje osobną kopię repozytorium bez edytowania plików." : "Agent może zmieniać kopię projektu i wykonywać polecenia na VM. Wynik zostanie do przejrzenia."));
bindForm("#project-form", async (values) => { const project = await post("/projects", values); $("#project-dialog").close(); $("#project-form").reset(); await loadProjects(); $("#task-project").value = project.id; toast("Projekt dodany. Możesz zlecić pierwsze zadanie."); });
bindForm("#task-form", async (values) => { values.timeout_minutes = Number(values.timeout_minutes); const job = await post("/jobs", values); $("#task-dialog").close(); $("#task-form").elements.title.value = ""; $("#task-form").elements.prompt.value = ""; await selectJob(job.id); await refresh(); toast("Zadanie dodane do kolejki."); });
$("#cancel-job").addEventListener("click", async () => { const id = state.selected; if (!id) return; $("#cancel-job").disabled = true; try { await post(`/jobs/${id}/cancel`); await refresh(); } catch (error) { $("#cancel-job").disabled = false; showError(error); } });
$("#retry-job").addEventListener("click", async () => { const id = state.selected; if (!id) return; $("#retry-job").disabled = true; try { const job = await post(`/jobs/${id}/retry`); await selectJob(job.id); await refresh(); toast("Utworzono nowe wykonanie zadania."); } catch (error) { showError(error); } finally { $("#retry-job").disabled = false; } });
$("#download-patch").addEventListener("click", async () => { const id = state.selected; if (!id) return; try { const response = await api(`/jobs/${id}/patch`); const url = URL.createObjectURL(await response.blob()); const anchor = element("a"); anchor.href = url; anchor.download = `task-${id.slice(0, 12)}.patch`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); } catch (error) { showError(error); } });
async function boot() { try { await Promise.all([loadProjects(), loadHealth()]); await refresh(); } catch (error) { showError(error); } finally { setInterval(refresh, 1800); } }
$("#logout").addEventListener("click", () => post("/logout").then(() => window.location.assign("/login")).catch(showError));
$("#import-jira").addEventListener("click", async () => {
  const button = $("#import-jira"), error = $("#task-form .form-error"); button.disabled = true; error.hidden = true;
  try { const issue = await json(`/jira/issues/${encodeURIComponent($("#jira-key").value.trim())}`); $("#task-form").elements.title.value = issue.title; $("#task-form").elements.prompt.value = issue.prompt; toast(`Pobrano ${issue.key}. Sprawdź opis przed uruchomieniem.`); }
  catch (exc) { error.textContent = exc.message; error.hidden = false; }
  finally { button.disabled = false; }
});
boot();
