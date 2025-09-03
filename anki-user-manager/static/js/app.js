console.log("Admin dashboard loaded");
<script>
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("table tbody tr").forEach(row => {
    const pwdCell = row.children[1];  // 2nd column = password
    const password = pwdCell.textContent.trim();

    // Replace password text with masked + toggle button
    pwdCell.innerHTML = `
      <span class="masked">********</span>
      <span class="real d-none">${password}</span>
      <button type="button" class="btn btn-sm btn-outline-secondary toggle-pass ms-2">
        <i class="bi bi-eye"></i>
      </button>
    `;
  });

  // Add toggle logic
  document.querySelectorAll(".toggle-pass").forEach(btn => {
    btn.addEventListener("click", () => {
      const cell = btn.closest("td");
      const masked = cell.querySelector(".masked");
      const real = cell.querySelector(".real");
      const icon = btn.querySelector("i");

      if (real.classList.contains("d-none")) {
        masked.classList.add("d-none");
        real.classList.remove("d-none");
        icon.classList.replace("bi-eye", "bi-eye-slash");
      } else {
        real.classList.add("d-none");
        masked.classList.remove("d-none");
        icon.classList.replace("bi-eye-slash", "bi-eye");
      }
    });
  });
});
</script>

