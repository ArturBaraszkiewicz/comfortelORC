"use strict";
document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget, button = form.querySelector("button"), error = form.querySelector(".form-error");
  button.disabled = true; error.hidden = true;
  try {
    const response = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json", "X-Orchestrator-Token": document.querySelector('meta[name="orchestrator-token"]').content }, body: JSON.stringify(Object.fromEntries(new FormData(form))) });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Nie udało się zalogować.");
    window.location.assign("/");
  } catch (exc) { error.textContent = exc.message; error.hidden = false; button.disabled = false; }
});
