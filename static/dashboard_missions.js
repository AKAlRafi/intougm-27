(() => {
  const showFeedback = (card, text, category) => {
    let feedback = card.querySelector(".mission-action-feedback");
    if (!feedback) {
      feedback = document.createElement("p");
      feedback.className = "mission-action-feedback";
      feedback.setAttribute("role", "status");
      feedback.setAttribute("aria-live", "polite");
      card.append(feedback);
    }
    feedback.textContent = text;
    feedback.dataset.state = category || "info";
  };

  const submitMissionForm = async (form, submitter) => {
    const card = form.closest(".mission-item");
    const buttons = [...form.querySelectorAll('button[type="submit"], button:not([type])')];
    const originalLabels = buttons.map((button) => button.textContent);
    const data = new FormData(form);
    if (submitter?.name) data.set(submitter.name, submitter.value);

    buttons.forEach((button) => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    });
    if (submitter) submitter.textContent = "Memproses…";
    showFeedback(card, form.matches(".evidence-form") ? "Mengirim bukti…" : "Menyimpan pemeriksaan…", "info");

    try {
      const response = await fetch(form.action, {
        method: form.method || "POST",
        body: data,
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.ok) {
        throw new Error(result.message || "Perubahan belum berhasil. Coba lagi.");
      }

      if (form.matches(".evidence-form")) {
        const status = card.querySelector(".mission-status");
        status.className = "mission-status pending";
        status.textContent = "Menunggu pemeriksaan";
        const link = document.createElement("a");
        link.className = "evidence-link";
        link.href = result.evidence_url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = "Lihat bukti yang dikirim ↗";
        form.before(link);
        form.remove();
      } else {
        const approved = data.get("decision") === "APPROVED";
        const status = card.querySelector(".mission-status");
        status.className = `mission-status ${approved ? "approved" : "rejected"}`;
        status.textContent = approved ? "Disetujui" : "Perlu diperbaiki";
        const count = document.querySelector(".quest-count");
        if (count) {
          const cards = [...document.querySelectorAll(".mission-item")];
          const approvedCount = cards.filter((item) =>
            item.querySelector(".mission-status")?.classList.contains("approved"),
          ).length;
          count.textContent = `${approvedCount}/${cards.length} disetujui`;
        }
        const note = form.querySelector('[name="review_note"]')?.value.trim();
        if (note) {
          const noteElement = document.createElement("p");
          noteElement.className = "review-note";
          const label = document.createElement("b");
          label.textContent = "Catatan admin: ";
          noteElement.append(label, document.createTextNode(note));
          form.before(noteElement);
        }
        form.remove();
      }
      showFeedback(card, result.message, result.category);
      return true;
    } catch (error) {
      showFeedback(card, error.message || "Koneksi gagal. Periksa jaringan lalu coba lagi.", "error");
      return false;
    } finally {
      buttons.forEach((button, index) => {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = originalLabels[index];
      });
    }
  };

  document.querySelectorAll(".evidence-form, .review-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void submitMissionForm(form, event.submitter);
    });
  });

})();
