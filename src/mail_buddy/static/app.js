"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const menuButton = document.querySelector("[data-menu-toggle]");
  const sidebar = document.querySelector(".sidebar");
  if (menuButton && sidebar) {
    menuButton.addEventListener("click", () => {
      const open = sidebar.classList.toggle("open");
      menuButton.setAttribute("aria-expanded", String(open));
      menuButton.textContent = open ? "Close" : "Menu";
    });
  }

  document.querySelectorAll("[data-dismiss]").forEach((button) => {
    button.addEventListener("click", () => button.parentElement?.remove());
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-preview-url]").forEach((trigger) => {
    const loadPreview = async () => {
      const target = trigger.parentElement?.querySelector(".message-preview");
      const url = trigger.getAttribute("data-preview-url");
      if (!target || !url) return;
      if (trigger instanceof HTMLButtonElement) {
        trigger.disabled = true;
        trigger.textContent = "Loading from Gmail…";
      }
      try {
        const response = await fetch(url, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        const preview = await response.json();
        if (!response.ok) throw new Error(preview.detail || "Preview unavailable");
        target.replaceChildren();
        const subjectLabel = document.createElement("small");
        subjectLabel.className = "message-field-label";
        subjectLabel.textContent = "Subject";
        const heading = document.createElement("strong");
        heading.textContent = preview.subject || "(No subject)";
        const sender = document.createElement("small");
        sender.textContent = `From: ${preview.sender || "unknown sender"}`;
        const bodyLabel = document.createElement("small");
        bodyLabel.className = "message-field-label";
        bodyLabel.textContent = "Body";
        const body = document.createElement("div");
        body.className = "message-body";
        body.textContent = preview.body || preview.content || "This message has no readable text content.";
        const attachmentText = document.createElement("div");
        attachmentText.className = "message-body attachment-text";
        if (preview.attachment_text) {
          const attachmentLabel = document.createElement("small");
          attachmentLabel.className = "message-field-label";
          attachmentLabel.textContent = "Extracted attachment text";
          attachmentText.textContent = preview.attachment_text;
          target.append(subjectLabel, heading, sender, bodyLabel, body, attachmentLabel, attachmentText);
        } else {
          target.append(subjectLabel, heading, sender, bodyLabel, body);
        }
        const link = document.createElement("a");
        link.href = preview.gmail_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Open this message in Gmail ↗";
        target.append(link);
        target.hidden = false;
        if (trigger instanceof HTMLButtonElement) {
          trigger.textContent = "Hide email";
          trigger.disabled = false;
          trigger.removeAttribute("data-preview-url");
          trigger.addEventListener("click", () => {
            target.hidden = !target.hidden;
            trigger.textContent = target.hidden ? "Show email" : "Hide email";
          });
        }
      } catch (error) {
        target.textContent = error instanceof Error ? error.message : "Preview unavailable";
        target.hidden = false;
        if (trigger instanceof HTMLButtonElement) {
          trigger.textContent = "Retry email";
          trigger.disabled = false;
          trigger.addEventListener("click", loadPreview, { once: true });
        }
      }
    };
    if (trigger instanceof HTMLButtonElement) {
      trigger.addEventListener("click", loadPreview, { once: true });
    } else {
      void loadPreview();
    }
  });
});
