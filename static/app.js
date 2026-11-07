(() => {
  // Animate back to the top after a refresh from the previous scroll position.
  window.addEventListener("pageshow", () => {
    const navigation = performance.getEntriesByType("navigation")[0];
    if (navigation?.type === "reload") window.scrollTo({ top: 0, behavior: "smooth" });
  });

  const sizes = { Single: 1, "Child-Free/Duo": 2, "Keluarga Berencana/Trio": 3, "Keluarga Besar/Circle": 5 };
  let prices = { Single: 50000, "Child-Free/Duo": 80000, "Keluarga Berencana/Trio": 145000, "Keluarga Besar/Circle": 225000 };
  try {
    prices = { ...prices, ...JSON.parse(document.body.dataset.packagePrices || "{}") };
  } catch (_error) {
    // Keep the embedded defaults if an older cached page has malformed price data.
  }
  const money = (value) =>
    new Intl.NumberFormat("id-ID", {
      style: "currency",
      currency: "IDR",
      maximumFractionDigits: 0,
    }).format(value);
  const authModal = document.querySelector("#auth-modal");
  const checkoutModal = document.querySelector("#checkout-modal");
  const paymentModal = document.querySelector("#payment-modal");
  const successModal = document.querySelector("#success-modal");
  const registrationSuccessModal = document.querySelector("#registration-success-modal");
  const guestStarModal = document.querySelector("#guest-star-modal");
  const mainEventModal = document.querySelector("#main-event-modal");
  const navAuth = document.querySelector("#nav-auth");
  const navRegister = document.querySelector("#nav-register");
  const navProfile = document.querySelector("#nav-profile");
  let csrfToken = "";
  let account = null;
  let selectedPackage = "Single";
  let pendingCheckoutPackage = null;
  let currentOrder = null;
  let pollTimer = null;
  let paidFollowupTimer = null;
  let voucherDiscount = 0;
  const referralFromUrl = new URLSearchParams(window.location.search).get("ref") || "";

  const csrf = async () => {
    const response = await fetch("/api/auth/csrf", { credentials: "same-origin" });
    const data = await response.json();
    csrfToken = data.csrf_token;
  };
  const api = async (url, body) => {
    try {
      if (!csrfToken) await csrf();
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok)
        throw Object.assign(new Error(data.message || "Belum berhasil. Coba lagi sebentar."), {
          data,
          status: response.status,
        });
      return data;
    } catch (error) {
      if (error instanceof TypeError)
        throw new Error("Koneksi terputus. Periksa internetmu, lalu coba lagi.");
      throw error;
    }
  };
  const message = (element, text, state = "") => {
    element.textContent = text;
    element.dataset.state = state;
  };
  const showAuth = (tab = "login", resumePackage = null) => {
    pendingCheckoutPackage = resumePackage;
    setAuthTab(tab);
    authModal.showModal();
  };
  const setAuthTab = (tab) => {
    document
      .querySelectorAll("[data-auth-tab]")
      .forEach((button) => button.classList.toggle("is-active", button.dataset.authTab === tab));
    document.querySelector("#login-form").hidden = tab !== "login";
    document.querySelector("#register-form").hidden = tab !== "register";
    document.querySelector("#auth-title").innerHTML =
      tab === "register"
        ? "Buat akun,<br /><em>Sobat InTO.</em>"
        : "Selamat datang,<br /><em>Sobat InTO.</em>";
  };
  const setAccount = (name, role = "") => {
    account = name ? { name, role } : null;
    navAuth.hidden = !!account;
    navRegister.hidden = !!account;
    navProfile.hidden = !account;
    document.querySelector("#nav-profile-name").textContent = account?.name || "";
    let dashboardLink = document.querySelector(".nav-dashboard");
    if (account?.role === "ba" || account?.role === "admin" || account?.role === "dev") {
      if (!dashboardLink) {
        dashboardLink = document.createElement("a");
        dashboardLink.className = "nav-dashboard";
        navProfile.before(dashboardLink);
      }
      const dashboard = account.role === "ba"
        ? { href: "/dashboardBA", label: "Dashboard Brand Ambassador" }
        : { href: "/dashboard-admin", label: "Dashboard Admin" };
      dashboardLink.href = dashboard.href;
      dashboardLink.textContent = dashboard.label;
      dashboardLink.hidden = false;
    } else if (dashboardLink) {
      if (dashboardLink.hasAttribute("data-server-rendered")) {
        dashboardLink.hidden = true;
      } else {
        dashboardLink.remove();
      }
    }
  };
  const formData = (form) =>
    Object.fromEntries([...new FormData(form).entries()].filter(([name]) => name !== "csrf_token"));

  document.querySelector(".menu-toggle").addEventListener("click", (event) => {
    const button = event.currentTarget;
    const open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", String(open));
    document.querySelector(".nav-links").classList.toggle("is-open", open);
  });
  document.querySelectorAll(".nav-links a, .nav-links button").forEach((link) =>
    link.addEventListener("click", () => {
      document.querySelector(".nav-links").classList.remove("is-open");
      document.querySelector(".menu-toggle").setAttribute("aria-expanded", "false");
    }),
  );
  const track = document.querySelector(".story-track");
  if (track) {
    const cards = [...track.querySelectorAll(".story")];
    const dots = [...document.querySelectorAll(".story-pagination button")];
    const setActiveDot = (index) =>
      dots.forEach((dot, dotIndex) => dot.setAttribute("aria-current", String(index === dotIndex)));
    dots.forEach((dot, index) =>
      dot.addEventListener("click", () => {
        const card = cards[index];
        if (!card) return;
        const left = card.getBoundingClientRect().left - track.getBoundingClientRect().left + track.scrollLeft;
        track.scrollTo({ left, behavior: "smooth" });
        setActiveDot(index);
      }),
    );
    let storyScrollFrame = 0;
    track.addEventListener("scroll", () => {
      if (storyScrollFrame) return;
      storyScrollFrame = window.requestAnimationFrame(() => {
        storyScrollFrame = 0;
        const left = track.getBoundingClientRect().left;
        let nearest = 0;
        cards.forEach((card, index) => {
          if (Math.abs(card.getBoundingClientRect().left - left) < Math.abs(cards[nearest].getBoundingClientRect().left - left)) {
            nearest = index;
          }
        });
        setActiveDot(nearest);
      });
    }, { passive: true });
    document
      .querySelector(".story-prev")
      ?.addEventListener("click", () =>
        track.scrollBy({ left: -track.clientWidth, behavior: "smooth" }),
      );
    document
      .querySelector(".story-next")
      ?.addEventListener("click", () =>
        track.scrollBy({ left: track.clientWidth, behavior: "smooth" }),
      );
  }
  document.querySelectorAll(".archive-photo").forEach((item) => {
    item.addEventListener("click", () =>
      document
        .querySelectorAll(".archive-photo")
        .forEach((photo) => photo.classList.toggle("is-active", photo === item)),
    );
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        item.click();
      }
    });
  });
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver(
      (entries) =>
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            observer.unobserve(entry.target);
          }
        }),
      { threshold: 0.14 },
    );
    document.querySelectorAll(".reveal").forEach((item, index) => {
      item.style.setProperty("--reveal-delay", `${(index % 4) * 100}ms`);
      observer.observe(item);
    });
  } else document.querySelectorAll(".reveal").forEach((item) => item.classList.add("is-visible"));

  async function logoutAccount() {
    try {
      await api("/api/auth/logout", {});
      setAccount(null);
      await csrf();
    } catch (error) {
      window.alert(error.message);
    }
  }
  navAuth.addEventListener("click", (event) => {
    event.preventDefault();
    showAuth("login");
  });
  navRegister.addEventListener("click", (event) => {
    event.preventDefault();
    showAuth("register");
  });
  navProfile.addEventListener("click", logoutAccount);
  document.querySelectorAll("[data-auth-tab]").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.preventDefault();
      setAuthTab(button.dataset.authTab);
    }),
  );
  document
    .querySelectorAll("[data-close]")
    .forEach((button) =>
      button.addEventListener("click", () => document.getElementById(button.dataset.close).close()),
    );
  const mainEventOpen = document.querySelector("#main-event-open");
  const mainEventOverview = document.querySelector("#main-event-overview");
  const mainEventDetail = document.querySelector("#main-event-detail");
  const resetMainEventModal = () => {
    if (!mainEventModal) return;
    mainEventOverview.hidden = false;
    mainEventDetail.hidden = true;
    mainEventDetail.querySelectorAll("[data-event-panel]").forEach((panel) => {
      panel.hidden = true;
    });
  };
  mainEventOpen?.addEventListener("click", () => {
    resetMainEventModal();
    mainEventModal.showModal();
  });
  mainEventModal?.querySelectorAll("[data-event-detail]").forEach((button) => {
    button.addEventListener("click", () => {
      const selected = button.dataset.eventDetail;
      mainEventOverview.hidden = true;
      mainEventDetail.hidden = false;
      mainEventDetail.querySelectorAll("[data-event-panel]").forEach((panel) => {
        panel.hidden = panel.dataset.eventPanel !== selected;
      });
      document.querySelector("#main-event-back").focus();
    });
  });
  document.querySelector("#main-event-back")?.addEventListener("click", () => {
    mainEventOverview.hidden = false;
    mainEventDetail.hidden = true;
  });
  mainEventModal?.addEventListener("close", resetMainEventModal);
  [
    authModal,
    registrationSuccessModal,
    checkoutModal,
    paymentModal,
    successModal,
    guestStarModal,
    mainEventModal,
  ].forEach((dialog) =>
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    }),
  );

  document.querySelector("#register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const status = form.querySelector(".form-message");
    const submitButton = form.querySelector(".register-submit");
    if (submitButton.disabled) return;
    const email = form.elements.email.value.trim();
    const defaultButtonContent = submitButton.innerHTML;
    message(status, "", "");
    submitButton.disabled = true;
    submitButton.setAttribute("aria-busy", "true");
    submitButton.innerHTML =
      '<span class="register-loading-state"><span class="loading-spinner" aria-hidden="true"></span>Memproses pendaftaran...</span><span aria-hidden="true">…</span>';
    try {
      const result = await api("/api/register", formData(form));
      message(status, result.message, "success");
      form.reset();
      // Keep the address handy so the user can request another link without
      // retyping it; registration itself never creates a logged-in session.
      document.querySelector("#login-form").elements.email.value = email;
      authModal.close();
      registrationSuccessModal.showModal();
    } catch (error) {
      if (error.status === 503 && email) {
        setAuthTab("login");
        const loginForm = document.querySelector("#login-form");
        loginForm.elements.email.value = email;
        message(loginForm.querySelector(".form-message"), error.message, "error");
      } else {
        message(status, error.message, "error");
      }
    } finally {
      submitButton.disabled = false;
      submitButton.removeAttribute("aria-busy");
      submitButton.innerHTML = defaultButtonContent;
    }
  });
  const registerForm = document.querySelector("#register-form");
  registerForm.querySelector(".register-submit").addEventListener("click", () => {
    message(registerForm.querySelector(".form-message"), "", "");
  });
  document.querySelector("#registration-success-login").addEventListener("click", () => {
    registrationSuccessModal.close();
    showAuth("login");
  });
  document.querySelector("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const status = form.querySelector(".form-message");
    try {
      const result = await api("/api/login", formData(form));
      setAccount(result.name, result.user?.role);
      csrfToken = result.csrf_token;
      authModal.close();
      const packageToResume = pendingCheckoutPackage;
      pendingCheckoutPackage = null;
      if (packageToResume) openCheckout(packageToResume);
    } catch (error) {
      message(status, error.message, "error");
    }
  });
  document.querySelector("#resend-verification").addEventListener("click", async () => {
    const form = document.querySelector("#login-form");
    const status = form.querySelector(".form-message");
    const email = form.elements.email.value.trim();
    if (!email) {
      message(status, "Isi alamat emailmu dulu, ya.", "error");
      return;
    }
    try {
      const result = await api("/api/auth/resend-verification", { email });
      message(status, result.message, "success");
    } catch (error) {
      message(status, error.message, "error");
    }
  });

  function renderParticipants() {
    const people = document.querySelector("#checkout-participants");
    people.innerHTML = Array.from({ length: sizes[selectedPackage] }, (_, index) => {
      const n = index + 1;
      const label = index === 0 ? "Pendaftar" : `Peserta ${n}`;
      return `
        <fieldset class="participant-card">
          <legend>${label}${index === 0 ? " · pendaftar utama" : ""}</legend>
          <label>
            Nama lengkap
            <input
              name="name-${n}"
              autocomplete="${n === 1 ? "name" : "off"}"
              required
              maxlength="120"
            />
          </label>
          <label>
            Asal sekolah
            <input name="school-${n}" required maxlength="140" />
          </label>
          <label>
              Alamat email peserta
            <input
              name="email-${n}"
              type="email"
              ${n === 1 ? 'readonly aria-describedby="buyer-email-note"' : ""}
              required
              maxlength="254"
            />
          </label>
          <label>
            No. WhatsApp
            <input
              name="phone-${n}"
              type="tel"
              autocomplete="${n === 1 ? "tel" : "off"}"
              required
              maxlength="24"
              placeholder="Contoh: 081234567890"
            />
          </label>
          ${
            n === 1
              ? `<small class="buyer-email-note" id="buyer-email-note">
                  Tiket pendaftar dikirim ke alamat email akunmu.
                </small>`
              : ""
          }
        </fieldset>`;
    }).join("");
    document.querySelector("#checkout-total").textContent = money(prices[selectedPackage]);
    document.querySelector("#voucher-code").value = "";
    document.querySelector("#voucher-message").textContent = "";
    document.querySelector("#checkout-message").textContent = "";
    voucherDiscount = 0;
  }
  async function openCheckout(packageName) {
    selectedPackage = packageName;
    document.querySelector("#checkout-package-name").textContent = packageName;
    renderParticipants();
    try {
      const currentPrice = await api("/api/vouchers/validate", { package: packageName, code: "" });
      if (Number.isFinite(currentPrice.base_price)) prices[packageName] = currentPrice.base_price;
      document.querySelector("#checkout-total").textContent = money(currentPrice.total);
    } catch (_error) {
      // The order endpoint will still apply the authoritative current price.
    }
    if (referralFromUrl) document.querySelector("#voucher-code").value = referralFromUrl;
    await hydrateBuyerEmail();
    const buyerEmailField = document.querySelector('[name="email-1"]');
    if (buyerEmailField) buyerEmailField.value = buyerEmail;
    checkoutModal.showModal();
    if (referralFromUrl) document.querySelector("#apply-voucher").click();
  }
  document.querySelectorAll(".purchase-trigger").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.preventDefault();
      selectedPackage = button.dataset.package;
      if (!account) {
        showAuth("login", selectedPackage);
        return;
      }
      openCheckout(selectedPackage);
    }),
  );

  document.querySelector("#apply-voucher").addEventListener("click", async () => {
    const status = document.querySelector("#voucher-message");
    const code = document.querySelector("#voucher-code").value.trim();
    if (!code) {
      voucherDiscount = 0;
      document.querySelector("#checkout-total").textContent = money(prices[selectedPackage]);
      message(status, "Isi kode vouchernya dulu, ya.", "error");
      return;
    }
    try {
      const result = await api("/api/vouchers/validate", { package: selectedPackage, code });
      if (Number.isFinite(result.base_price)) prices[selectedPackage] = result.base_price;
      voucherDiscount = result.discount_amount;
      document.querySelector("#checkout-total").textContent = money(result.total);
      message(status, result.message, result.valid ? "success" : "error");
    } catch (error) {
      message(status, error.message, "error");
    }
  });

  document.querySelector("#voucher-code").addEventListener("blur", async (event) => {
    if (event.currentTarget.value.trim()) document.querySelector("#apply-voucher").click();
  });

  let buyerEmail = "";
  async function hydrateBuyerEmail() {
    // Read the authenticated buyer's email; server continues to enforce this on order creation.
    try {
      const response = await fetch("/api/auth/me", { credentials: "same-origin" });
      const data = await response.json();
      buyerEmail = data.email || "";
    } catch (_) {
      buyerEmail = "";
    }
  }
  async function loadSnap(url) {
    if (window.snap) return;
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.setAttribute("data-client-key", window.intoSnapClientKey || "");
      script.onload = resolve;
      script.onerror = () => reject(new Error("Pilihan pembayaran belum terbuka. Coba muat ulang halaman."));
      document.head.appendChild(script);
    });
  }
  function ensureSnapContainer() {
    let container = document.querySelector("#snap-container");
    if (container) return container;

    // A running Flask process can keep an older compiled Jinja template in
    // memory while serving the newly refreshed JavaScript and CSS. Upgrade
    // that older payment modal in place instead of failing on a missing node.
    const actions = document.querySelector("#payment-modal .payment-actions");
    if (!actions) return null;
    document.querySelector("#open-snap")?.remove();

    const shell = document.createElement("div");
    shell.className = "snap-embed-shell";
    shell.setAttribute("aria-label", "Pilihan metode dan instruksi pembayaran Midtrans");
    container = document.createElement("div");
    container.id = "snap-container";
    shell.append(container);
    actions.before(shell);

    const lead = document.querySelector("#payment-modal .modal-lead");
    if (lead) {
      lead.textContent =
        "Pilih QRIS, akun virtual, atau dompet digital di bawah ini. " +
        "Status pembayaran akan diperbarui otomatis. Kamu juga bisa mengeceknya kapan saja.";
    }
    return container;
  }
  function participantsFromForm() {
    return Array.from({ length: sizes[selectedPackage] }, (_, index) => {
      const n = index + 1;
      return {
        full_name: document.querySelector(`[name="name-${n}"]`).value.trim(),
        school: document.querySelector(`[name="school-${n}"]`).value.trim(),
        email: n === 1 ? buyerEmail : document.querySelector(`[name="email-${n}"]`).value.trim(),
        phone: document.querySelector(`[name="phone-${n}"]`).value.trim(),
      };
    });
  }
  document.querySelector("#checkout-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const status = document.querySelector("#checkout-message");
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      await hydrateBuyerEmail();
      const payload = {
        package: selectedPackage,
        voucher_code: document.querySelector("#voucher-code").value.trim(),
        participants: participantsFromForm(),
      };
      const result = await api("/api/orders", payload);
      currentOrder = result.order_id;
      checkoutModal.close();
      document.querySelector("#payment-order-id").textContent = result.order_id;
      document.querySelector("#payment-order-amount").textContent = money(result.amount);
      document.querySelector("#payment-order-status").textContent = "Menunggu pembayaran";
      document.querySelector("#payment-message").textContent = "";
      window.intoSnapClientKey = result.snap_client_key;
      paymentModal.showModal();
      if (
        payload.voucher_code &&
        result.voucher_message &&
        result.voucher_message !== "Kode voucher berhasil dipakai."
      ) {
        message(document.querySelector("#payment-message"), result.voucher_message, "error");
      }
      try {
        await mountSnap(result.snap_url, result.snap_token);
      } catch (error) {
        message(document.querySelector("#payment-message"), error.message, "error");
      }
      pollStatus();
    } catch (error) {
      if (error.status === 401) {
        checkoutModal.close();
        showAuth("login", selectedPackage);
      } else message(status, error.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  async function mountSnap(url, token) {
    if (!token || !url) {
      message(
        document.querySelector("#payment-message"),
        "Pembayaran belum bisa dibuka. Muat ulang halaman, lalu coba lagi.",
        "error",
      );
      return;
    }
    try {
      await loadSnap(url);
    } catch (error) {
      message(document.querySelector("#payment-message"), error.message, "error");
      return;
    }
    const container = ensureSnapContainer();
    if (!container)
      throw new Error("Pilihan pembayaran belum muncul. Muat ulang halaman, lalu coba lagi.");
    container.replaceChildren();
    window.snap.embed(token, {
      embedId: "snap-container",
      onSuccess: () => pollStatus(true),
      onPending: () => pollStatus(true),
      onError: () =>
        message(
          document.querySelector("#payment-message"),
          "Pembayaran belum berhasil. Periksa kembali detailnya atau pilih metode lain.",
          "error",
        ),
      onClose: () => pollStatus(true),
    });
  }
  document.querySelector("#check-status").addEventListener("click", () => pollStatus(true));
  function renderPaidStatus(data) {
    const lead = document.querySelector("#success-modal .modal-lead");
    const messages = [];
    messages.push(
      data.sheets_synced
        ? "Data peserta sudah tercatat."
        : "Pembayaran berhasil. Data peserta sedang kami proses; kamu tidak perlu membayar lagi.",
    );
    messages.push(
      data.email_sent
        ? "Tiket sudah dikirim ke alamat email peserta."
        : "Tiket sedang disiapkan dan akan dikirim ke alamat email peserta.",
    );
    lead.textContent = messages.join(" ");
    document.querySelector("#ticket-list").innerHTML = (data.tickets || [])
      .map(
        (ticket) =>
          `<div class="ticket-item">
            <span>${escapeHtml(ticket.full_name)}</span>
          <strong>${escapeHtml(ticket.ticket_code || "Dikirim ke alamat email")}</strong>
          </div>`,
      )
      .join("");
  }
  async function retryPaidFollowups() {
    if (!currentOrder || !account) return;
    try {
      const response = await fetch(`/api/orders/${encodeURIComponent(currentOrder)}`, {
        credentials: "same-origin",
      });
      const data = await response.json();
      if (!response.ok || data.order?.status !== "PAID") return;
      renderPaidStatus(data);
      if (data.sheets_synced && data.email_sent) {
        clearInterval(paidFollowupTimer);
        paidFollowupTimer = null;
      }
    } catch (_error) {
      // Keep retrying while the paid order's email or Sheets delivery is pending.
    }
  }
  async function pollStatus(showError = false) {
    if (!currentOrder || !account) return;
    try {
      const response = await fetch(`/api/orders/${encodeURIComponent(currentOrder)}`, {
        credentials: "same-origin",
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "Status pembayaran belum bisa diperiksa.");
      const order = data.order;
      const statusLabels = {
        PENDING: "Menunggu pembayaran",
        PAID: "Pembayaran berhasil",
        EXPIRED: "Waktu pembayaran habis",
        CANCELLED: "Pembayaran dibatalkan",
        DENIED: "Pembayaran ditolak",
        FAILED: "Pembayaran gagal",
      };
      document.querySelector("#payment-order-status").textContent =
        statusLabels[order.status] || "Status sedang diperbarui";
      if (order.status === "PAID") {
        clearInterval(pollTimer);
        paymentModal.close();
        renderPaidStatus(data);
        successModal.showModal();
        if ((!data.sheets_synced || !data.email_sent) && !paidFollowupTimer) {
          paidFollowupTimer = setInterval(retryPaidFollowups, 5000);
        }
        return;
      }
      if (["EXPIRED", "CANCELLED", "DENIED", "FAILED"].includes(order.status)) {
        document.querySelector("#payment-order-status").textContent =
          statusLabels[order.status] || "Status sedang diperbarui";
        clearInterval(pollTimer);
      }
    } catch (error) {
      if (showError) {
        const friendlyError =
          error instanceof TypeError
            ? "Koneksi terputus. Periksa internetmu, lalu coba cek pembayaran lagi."
            : error.message;
        message(document.querySelector("#payment-message"), friendlyError, "error");
      }
    }
  }
  paymentModal.addEventListener("close", () => clearInterval(pollTimer));
  function escapeHtml(value) {
    const element = document.createElement("span");
    element.textContent = value || "";
    return element.innerHTML;
  }
  setInterval(() => {
    if (currentOrder && paymentModal.open) pollStatus();
  }, 4000);

  pendingCheckoutPackage = document.body.dataset.pendingPackage || null;
  if (document.body.dataset.authView) {
    setAuthTab(document.body.dataset.authView);
    if (authModal.open) authModal.close();
    authModal.showModal();
  } else if (!pendingCheckoutPackage && guestStarModal) {
    window.setTimeout(() => {
      if (document.querySelector("dialog[open]")) return;
      guestStarModal.showModal();
    }, 450);
  }

  csrf()
    .then(async () => {
      const response = await fetch("/api/auth/me", { credentials: "same-origin" });
      const data = await response.json();
      if (data.authenticated) setAccount(data.name, data.role);
    })
    .catch(() => {});
})();
