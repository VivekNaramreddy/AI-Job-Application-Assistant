// Dashboard Frontend JavaScript Application Logic
const guidedFields = [
  "target_roles",
  "target_locations",
  "required_keywords",
  "preferred_keywords",
  "excluded_keywords"
];
const guidedSelections = Object.fromEntries(guidedFields.map(field => [field, []]));
const suggestionRequestIds = {};

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initGuidedFields();
  loadProfile();
  renderDashboardJobs();
  loadDashboardJobs();
  setInterval(loadDashboardJobs, 5000);
  loadRuntimeStatus();
  initWebSocket();

  // Attach button event handlers
  document.getElementById("btn-search-save").addEventListener("click", startSearchAndSave);
  loadSearchStatus();
  setInterval(loadSearchStatus, 3000);
  document.getElementById("btn-start").addEventListener("click", startAutopilot);
  document.getElementById("btn-reviewed-excel").addEventListener("click", () => downloadExcel("reviewed"));
  document.getElementById("btn-applied-excel").addEventListener("click", () => downloadExcel("applied"));
  document.getElementById("btn-refresh-reviews").addEventListener("click", loadApplicationReviews);
  document.getElementById("btn-refresh-search-issues").addEventListener("click", loadSearchIssues);
  document.getElementById("btn-refresh-applied").addEventListener("click", loadAppliedJobs);
  document.getElementById("btn-upload-resume").addEventListener("click", uploadResume);
  document.getElementById("btn-clear-logs").addEventListener("click", () => {
    document.getElementById("log-output").innerHTML = '';
  });

  // Forms submit handlers
  document.getElementById("form-search").addEventListener("submit", saveSearchCriteria);
  document.getElementById("form-profile").addEventListener("submit", saveUserProfile);
});

// Tab Switcher
function initTabs() {
  const tabs = document.querySelectorAll(".nav-item");
  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      tabs.forEach(t => t.classList.remove("active"));
      document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
      tab.classList.add("active");
      const targetId = tab.getAttribute("data-tab");
      document.getElementById(targetId).classList.add("active");
    });
  });
}

function initGuidedFields() {
  guidedFields.forEach(field => {
    const input = document.getElementById(field);
    input.addEventListener("input", () => loadSuggestions(field, input.value));
    input.addEventListener("focus", () => loadSuggestions(field, input.value));
    input.addEventListener("keydown", event => {
      const menu = document.getElementById(`${field}_suggestions`);
      const firstOption = menu.querySelector("button");
      if (event.key === "ArrowDown" && firstOption) {
        event.preventDefault();
        firstOption.focus();
      } else if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        if (firstOption) {
          firstOption.click();
        } else if (input.value.trim()) {
          addGuidedValue(field, input.value.trim());
        }
      } else if (event.key === "Backspace" && !input.value && guidedSelections[field].length) {
        removeGuidedValue(field, guidedSelections[field].at(-1));
      } else if (event.key === "Escape") {
        menu.hidden = true;
      }
    });
  });

  document.addEventListener("click", event => {
    if (!event.target.closest(".guided-select")) {
      document.querySelectorAll(".suggestion-menu").forEach(menu => {
        menu.hidden = true;
      });
    }
  });
}

async function loadSuggestions(field, query) {
  const requestId = (suggestionRequestIds[field] || 0) + 1;
  suggestionRequestIds[field] = requestId;
  try {
    const res = await fetch(`/api/suggestions/${field}?q=${encodeURIComponent(query.trim())}&limit=10`);
    if (!res.ok || suggestionRequestIds[field] !== requestId) return;
    const data = await res.json();
    renderSuggestionMenu(field, data.suggestions || [], query.trim());
  } catch (err) {
    console.error(`Error loading ${field} suggestions:`, err);
  }
}

function renderSuggestionMenu(field, suggestions, query) {
  const menu = document.getElementById(`${field}_suggestions`);
  menu.replaceChildren();
  const selected = new Set(guidedSelections[field].map(value => value.toLowerCase()));
  const available = suggestions.filter(value => !selected.has(value.toLowerCase()));

  available.forEach(value => {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "suggestion-option";
    option.textContent = value;
    option.addEventListener("click", () => addGuidedValue(field, value));
    menu.appendChild(option);
  });

  if (query && !selected.has(query.toLowerCase()) && !suggestions.some(value => value.toLowerCase() === query.toLowerCase())) {
    const customOption = document.createElement("button");
    customOption.type = "button";
    customOption.className = "suggestion-option custom-option";
    customOption.textContent = `Use custom value: ${query}`;
    customOption.addEventListener("click", () => addGuidedValue(field, query));
    menu.appendChild(customOption);
  }

  menu.hidden = menu.childElementCount === 0;
}

function addGuidedValue(field, value) {
  const cleaned = value.trim();
  if (!cleaned || guidedSelections[field].some(item => item.toLowerCase() === cleaned.toLowerCase())) {
    return;
  }
  guidedSelections[field].push(cleaned);
  document.getElementById(field).value = "";
  document.getElementById(`${field}_suggestions`).hidden = true;
  renderGuidedChips(field);
}

function removeGuidedValue(field, value) {
  guidedSelections[field] = guidedSelections[field].filter(item => item !== value);
  renderGuidedChips(field);
}

function setGuidedValues(field, values) {
  guidedSelections[field] = Array.from(new Set((values || []).map(value => String(value).trim()).filter(Boolean)));
  document.getElementById(field).value = "";
  renderGuidedChips(field);
}

function getGuidedValues(field) {
  return [...guidedSelections[field]];
}

function renderGuidedChips(field) {
  const container = document.getElementById(`${field}_chips`);
  container.replaceChildren();
  guidedSelections[field].forEach(value => {
    const chip = document.createElement("span");
    chip.className = "selection-chip";
    chip.append(document.createTextNode(value));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${value}`);
    remove.textContent = "×";
    remove.addEventListener("click", () => removeGuidedValue(field, value));
    chip.appendChild(remove);
    container.appendChild(chip);
  });
}

// Fetch & Populate Profile & Search Settings
async function loadProfile() {
  try {
    const res = await fetch("/api/profile");
    const data = await res.json();
    
    // Personal Info
    const info = data.personal_info || {};
    document.getElementById("full_name").value = info.full_name || "";
    document.getElementById("email").value = info.email || "";
    document.getElementById("phone").value = info.phone || "";
    document.getElementById("city_state").value = info.city_state || "";
    document.getElementById("country").value = info.country || "";
    document.getElementById("linkedin_url").value = info.linkedin_url || "";
    document.getElementById("portfolio_url").value = info.portfolio_url || "";
    document.getElementById("resume_path").value = info.resume_path || "";
    const resumeName = (info.resume_path || "").split(/[\\/]/).pop();
    document.getElementById("resume-upload-status").innerText = resumeName
      ? `Current resume: ${resumeName}`
      : "No uploaded resume selected.";

    // Default Answers
    const answers = data.default_answers || {};
    document.getElementById("years_exp").value = answers.years_of_experience || "";
    document.getElementById("work_auth").value = answers.work_authorization || "";
    document.getElementById("sponsorship").value = answers.requires_sponsorship || "";
    document.getElementById("desired_salary").value = answers.desired_salary || "";
    document.getElementById("education_level").value = answers.education_level || "";
    document.getElementById("notice_period").value = answers.notice_period_weeks || "";

    const ai = data.ai_settings || {};
    document.getElementById("llm_enabled").checked = Boolean(ai.enabled);
    document.getElementById("llm_provider").value = ai.provider || "none";
    document.getElementById("llm_base_url").value = ai.base_url || "";
    document.getElementById("llm_model").value = ai.model || "";
    document.getElementById("llm_confidence").value = ai.minimum_confidence ?? 0.85;
    document.getElementById("llm_timeout").value = ai.timeout_seconds ?? 10;

    // Search Preferences
    const prefs = data.work_preferences || {};
    setGuidedValues("target_roles", prefs.target_roles || []);
    setGuidedValues("target_locations", prefs.target_locations || []);
    const workplaceTypes = prefs.workplace_types || ["Remote", "Hybrid", "On-site"];
    document.querySelectorAll("input[name='workplace_type']").forEach(input => {
      input.checked = workplaceTypes.includes(input.value);
    });
    const jobTypes = prefs.job_types || ["Full-time"];
    document.querySelectorAll("input[name='job_type']").forEach(input => {
      input.checked = jobTypes.includes(input.value);
    });
    setGuidedValues("required_keywords", prefs.required_keywords || []);
    setGuidedValues("preferred_keywords", prefs.preferred_keywords || []);
    setGuidedValues("excluded_keywords", prefs.excluded_keywords || []);
    document.getElementById("excluded_companies").value = (prefs.excluded_companies || []).join(", ");
    document.getElementById("experience_years_range").value = prefs.experience_years_range || "Any";
    document.getElementById("experience_level").value = prefs.experience_level || "Any";
    document.getElementById("max_applications").value = prefs.max_applications_per_run || 25;
    document.getElementById("delay_sec").value = prefs.delay_between_applications_sec || 5;

  } catch (err) {
    appendLog(`❌ Error loading profile: ${err}`, "system");
  }
}

// Save Search Criteria
async function saveSearchCriteria(e) {
  e.preventDefault();
  const roles = getGuidedValues("target_roles");
  const locs = getGuidedValues("target_locations");
  const workplaceTypes = Array.from(document.querySelectorAll("input[name='workplace_type']:checked"), input => input.value);
  const jobTypes = Array.from(document.querySelectorAll("input[name='job_type']:checked"), input => input.value);
  const requiredKeywords = getGuidedValues("required_keywords");
  const preferredKeywords = getGuidedValues("preferred_keywords");
  const excludedKeywords = getGuidedValues("excluded_keywords");
  const excludedCompanies = splitCommaList("excluded_companies");
  if (!roles.length) {
    alert("Please enter at least one target job title.");
    return;
  }
  if (!workplaceTypes.length) {
    alert("Please select at least one workplace type.");
    return;
  }
  if (!jobTypes.length) {
    alert("Please select at least one employment type.");
    return;
  }
  const maxApps = parseInt(document.getElementById("max_applications").value, 10);
  const delay = parseInt(document.getElementById("delay_sec").value, 10);

  const payload = {
    personal_info: {},
    work_preferences: {
      target_roles: roles,
      target_locations: locs.length ? locs : ["United States"],
      workplace_types: workplaceTypes,
      job_types: jobTypes,
      experience_years_range: document.getElementById("experience_years_range").value,
      experience_level: document.getElementById("experience_level").value,
      easy_apply_only: false,
      parallel_google_search: false,
      required_keywords: requiredKeywords,
      preferred_keywords: preferredKeywords,
      excluded_keywords: excludedKeywords,
      excluded_companies: excludedCompanies,
      max_applications_per_run: maxApps,
      delay_between_applications_sec: delay
    },
    default_answers: {}
  };

  await sendProfileUpdate(payload, "Search criteria saved successfully!");
}

function splitCommaList(elementId) {
  return document.getElementById(elementId).value
    .split(",")
    .map(value => value.trim())
    .filter(Boolean);
}

// Save User Profile
async function saveUserProfile(e) {
  e.preventDefault();
  const payload = {
    personal_info: {
      full_name: document.getElementById("full_name").value,
      email: document.getElementById("email").value,
      phone: document.getElementById("phone").value,
      city_state: document.getElementById("city_state").value,
      country: document.getElementById("country").value,
      linkedin_url: document.getElementById("linkedin_url").value,
      portfolio_url: document.getElementById("portfolio_url").value,
      resume_path: document.getElementById("resume_path").value
    },
    work_preferences: {},
    default_answers: {
      years_of_experience: document.getElementById("years_exp").value,
      work_authorization: document.getElementById("work_auth").value,
      requires_sponsorship: document.getElementById("sponsorship").value,
      desired_salary: document.getElementById("desired_salary").value,
      education_level: document.getElementById("education_level").value,
      notice_period_weeks: document.getElementById("notice_period").value
    },
    ai_settings: {
      enabled: document.getElementById("llm_enabled").checked,
      provider: document.getElementById("llm_provider").value,
      base_url: document.getElementById("llm_base_url").value,
      model: document.getElementById("llm_model").value,
      minimum_confidence: parseFloat(document.getElementById("llm_confidence").value || "0.85"),
      timeout_seconds: parseInt(document.getElementById("llm_timeout").value || "10", 10)
    }
  };

  await sendProfileUpdate(payload, "User profile saved successfully!");
}

async function uploadResume() {
  const input = document.getElementById("resume_upload");
  const button = document.getElementById("btn-upload-resume");
  const status = document.getElementById("resume-upload-status");
  const file = input.files[0];
  if (!file) {
    alert("Choose a PDF, DOC, or DOCX resume first.");
    return;
  }
  button.disabled = true;
  status.innerText = `Uploading ${file.name}...`;
  try {
    const res = await fetch("/api/profile/resume", {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Resume-Filename": encodeURIComponent(file.name)
      },
      body: file
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Resume upload failed.");
    }
    document.getElementById("resume_path").value = data.resume_path;
    status.innerText = `Current resume: ${data.filename}`;
    appendLog(`Resume uploaded: ${data.filename}`, "system");
    input.value = "";
  } catch (err) {
    status.innerText = "Upload failed.";
    alert(`Resume upload error: ${err.message || err}`);
  } finally {
    button.disabled = false;
  }
}

async function sendProfileUpdate(payload, successMsg) {
  try {
    const res = await fetch("/api/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      alert(successMsg);
      appendLog(`✅ ${successMsg}`, "system");
    }
  } catch (err) {
    alert(`Error saving: ${err}`);
  }
}

const rejectionReasons = [["no_longer_accepting", "No longer accepting applications"], ["position_closed", "Position closed / filled"], ["job_expired", "Job expired"], ["not_eligible", "Not eligible / work authorization"], ["location_not_suitable", "Location not suitable"], ["experience_too_high", "Experience requirement too high"], ["skills_mismatch", "Skills mismatch"], ["education_mismatch", "Education mismatch"], ["compensation_not_suitable", "Salary/compensation not suitable"], ["work_arrangement_not_suitable", "Work arrangement not suitable"], ["duplicate_job", "Duplicate job"], ["already_applied_elsewhere", "Already applied elsewhere"], ["description_changed", "Job description changed"], ["company_recruiter_issue", "Company/recruiter issue"], ["not_interested", "Not interested"], ["other", "Other"]];
const rejectionDrafts = new Map();

function jobStatusControl(job) {
  const automatic = ["skipped", "failed"].includes(jobBucket(job));
  const draft = rejectionDrafts.get(job.job_url);
  const status = draft ? "Rejected" : automatic ? "" : job.job_status;
  const rejectionLabel = (job.rejection_reason === "other" ? job.rejection_feedback?.trim() : "")
    || rejectionReasons.find(([value]) => value === job.rejection_reason)?.[1]
    || "No rejection reason recorded";
  return `<div class="job-status-control"><label>Job status:
    <select class="btn-sm" aria-label="Job status" data-job-url="${escapeHtml(job.job_url)}"
      data-previous-status="${automatic ? "" : job.job_status}" onchange="setManualJobStatus(this)">
      ${automatic && !draft ? `<option value="" selected disabled>${jobBucket(job) === "skipped" ? "Skipped" : "Failed / Flagged"}</option>` : ""}
      ${["Pending", "Applied", "Rejected"].map(value => `<option value="${value}" ${status === value ? "selected" : ""}>${value}</option>`).join("")}
    </select>
  </label>
  <span class="status-updated" aria-live="polite">${job.status_changed_at ? `Updated: ${escapeHtml(job.status_changed_at)}` : ""}</span>
  ${job.job_status === "Rejected" && !draft ? `<span class="rejection-summary">Rejected reason: ${escapeHtml(rejectionLabel)}</span>
    <button class="btn-sm" type="button" onclick="openRejectionEditor(this)">Edit rejection</button>` : ""}
  ${draft ? rejectionEditor(draft) : ""}</div>`;
}

function rejectionEditor(draft) {
  return `<form class="rejection-editor" onsubmit="saveRejection(event, this)">
    <label>Rejection Reason
      <select name="rejection_reason" required onchange="updateRejectionDraft(this)">
        <option value="">Select a reason</option>
        ${rejectionReasons.map(([value, label]) => `<option value="${value}" ${draft.rejection_reason === value ? "selected" : ""}>${label}</option>`).join("")}
      </select>
    </label>
    <label>Additional feedback / notes (optional)
      <input name="rejection_feedback" type="text" value="${escapeHtml(draft.rejection_feedback)}"
        placeholder="Posting says remote, but description requires 3 days onsite."
        oninput="updateRejectionDraft(this)">
    </label>
    <div><button class="btn-sm" type="submit">Save rejection</button>
      <button class="btn-sm" type="button" onclick="cancelRejection(this)">Cancel</button></div>
    <span class="rejection-error" role="alert"></span>
  </form>`;
}

function openRejectionEditor(control) {
  const wrapper = control.closest(".job-status-control");
  const select = wrapper.querySelector("select[data-job-url]");
  const job = dashboardJobs.find(job => job.job_url === select.dataset.jobUrl);
  const draft = rejectionDrafts.get(job.job_url) || {rejection_reason: job.rejection_reason || "", rejection_feedback: job.rejection_feedback || ""};
  rejectionDrafts.set(job.job_url, draft);
  select.value = "Rejected";
  if (!wrapper.querySelector("form")) wrapper.insertAdjacentHTML("beforeend", rejectionEditor(draft));
  wrapper.querySelector('[name="rejection_reason"]').focus();
}

function updateRejectionDraft(input) {
  const url = input.closest(".job-status-control").querySelector("select[data-job-url]").dataset.jobUrl;
  rejectionDrafts.get(url)[input.name] = input.value;
}

function cancelRejection(button) {
  const url = button.closest(".job-status-control").querySelector("select[data-job-url]").dataset.jobUrl;
  rejectionDrafts.delete(url);
  renderDashboardJobs();
}

function saveRejection(event, form) {
  event.preventDefault();
  const select = form.closest(".job-status-control").querySelector("select[data-job-url]");
  setManualJobStatus(select, true);
}

async function setManualJobStatus(select, saveRejectionDetails = false) {
  if (select.value === "Rejected" && !saveRejectionDetails) {
    openRejectionEditor(select);
    return;
  }
  const draft = saveRejectionDetails ? rejectionDrafts.get(select.dataset.jobUrl) : null;
  if (!draft) {
    rejectionDrafts.delete(select.dataset.jobUrl);
    select.closest(".job-status-control").querySelector("form")?.remove();
  }
  const form = select.closest(".job-status-control").querySelector("form");
  if (form) form.querySelectorAll("input, select, button").forEach(control => control.disabled = true);
  select.disabled = true;
  try {
    const response = await fetch("/api/jobs/status", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({job_url: select.dataset.jobUrl, status: select.value, ...(draft || {})})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not save job status");
    // The successful write is authoritative; do not wait for another GET.
    ++dashboardRequest;
    const job = dashboardJobs.find(job => job.job_url === result.job_url);
    if (job) {
      job.job_status = result.job_status;
      job.tracking_status = result.job_status;
      job.status_changed_at = result.status_changed_at;
      job.rejection_reason = result.rejection_reason;
      job.rejection_feedback = result.rejection_feedback;
      job.deletion_protected = result.deletion_protected;
      if (job.review && result.job_status !== "Pending") job.review.status = "rejected";
    }
    rejectionDrafts.delete(select.dataset.jobUrl);
    renderDashboardJobs();
  } catch (error) {
    if (draft && form) {
      form.querySelector(".rejection-error").textContent = `Rejection was not saved: ${error.message || error}`;
      return;
    }
    select.value = select.dataset.previousStatus;
    alert(`Job status was not saved: ${error.message || error}`);
  } finally {
    select.disabled = false;
    if (form) form.querySelectorAll("input, select, button").forEach(control => control.disabled = false);
  }
}

// All tabs derive from the same snapshot; source filters never mutate jobs.
let dashboardJobs = [];
let dashboardRequest = 0;
const sourceFilters = {};
const methodFilters = {};
const selectedJobs = new Set();
let deletingJobs = false;
const jobViews = {
  review: ["review-container", "badge-reviews", "tab-reviews"],
  applied: ["applied-container", "badge-applied", "tab-applied"],
  rejected: ["rejected-container", "badge-rejected", "tab-rejected"],
  skipped: ["search-issues-container", "badge-search-issues", "tab-search-issues"],
  failed: ["flagged-container", "badge-flagged", "tab-flagged"]
};

function jobBucket(job) {
  return {pending: "review", applied: "applied", rejected: "rejected", skipped: "skipped", failed: "failed"}[
    String(job.job_status || "").trim().toLowerCase()
  ];
}

async function loadDashboardJobs() {
  const request = ++dashboardRequest;
  try {
    const response = await fetch("/api/jobs/dashboard");
    if (!response.ok) throw new Error("Could not load job history");
    const jobs = await response.json();
    if (request !== dashboardRequest) return;
    if (JSON.stringify(jobs) === JSON.stringify(dashboardJobs) && document.getElementById("source-filters-review")) return;
    dashboardJobs = jobs;
    renderDashboardJobs();
  } catch (error) {
    console.error("Error loading jobs:", error);
  }
}

function selectJobSource(bucket, source) {
  sourceFilters[bucket] = source;
  selectedJobs.clear();
  renderDashboardJobs();
}

function selectJobMethod(bucket, method) {
  methodFilters[bucket] = method;
  selectedJobs.clear();
  renderDashboardJobs();
}

function visibleJobs(bucket) {
  return dashboardJobs.filter(job => jobBucket(job) === bucket
    && (!sourceFilters[bucket] || sourceFilters[bucket] === "all" || job.source_mode === sourceFilters[bucket])
    && (!methodFilters[bucket] || methodFilters[bucket] === "all" || job.application_method === methodFilters[bucket]));
}

function selectJobForDeletion(input) {
  if (input.checked) selectedJobs.add(input.dataset.jobUrl);
  else selectedJobs.delete(input.dataset.jobUrl);
  renderDashboardJobs();
}

async function deleteJobs(bucket, action, url = null) {
  if (bucket === "applied" || deletingJobs) return;
  const jobs = visibleJobs(bucket).filter(job => !job.deletion_protected
    && (action === "filtered" || (action === "single" ? job.job_url === url : selectedJobs.has(job.job_url))));
  if (!jobs.length) return;
  if (!window.confirm(`Delete ${jobs.length} ${action === "filtered" ? "filtered " : ""}job${jobs.length === 1 ? "" : "s"}? This cannot be undone.`)) return;
  deletingJobs = true;
  renderDashboardJobs();
  try {
    const response = await fetch("/api/jobs/delete", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({job_urls: jobs.map(job => job.job_url), bucket,
        source_mode: sourceFilters[bucket] || "all", application_method: methodFilters[bucket] || "all"})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Could not delete jobs");
    ++dashboardRequest;
    const deleted = new Set(result.deleted_job_urls);
    dashboardJobs = dashboardJobs.filter(job => !deleted.has(job.job_url));
    for (const url of deleted) {
      selectedJobs.delete(url);
      rejectionDrafts.delete(url);
    }
  } catch (error) {
    alert(`Jobs were not deleted: ${error.message || error}`);
  } finally {
    deletingJobs = false;
    renderDashboardJobs();
  }
}

function renderDashboardJobs() {
  for (const [bucket, [containerId, badgeId, tabId]] of Object.entries(jobViews)) {
    const jobs = dashboardJobs.filter(job => jobBucket(job) === bucket);
    document.getElementById(badgeId).innerText = jobs.length;
    let filters = document.getElementById(`source-filters-${bucket}`);
    if (!filters) {
      filters = document.createElement("div");
      filters.id = `source-filters-${bucket}`;
      filters.className = "source-filters";
      filters.setAttribute("aria-label", "Filter jobs by source");
      const title = document.getElementById(tabId).querySelector(".tab-header, h2");
      title.insertAdjacentElement("afterend", filters);
    }
    const selected = sourceFilters[bucket] || "all";
    const method = methodFilters[bucket] || "all";
    const methodJobs = jobs.filter(job => method === "all" || job.application_method === method);
    filters.innerHTML = [["all", "All"], ["autopilot", "Autopilot"], ["search_save", "Search & Save"]].map(([source, label]) => {
      const count = source === "all" ? methodJobs.length : methodJobs.filter(job => job.source_mode === source).length;
      return `<button class="btn-sm" aria-pressed="${selected === source}" onclick="selectJobSource('${bucket}', '${source}')">${label} (${count})</button>`;
    }).join("");
    let methods = document.getElementById(`method-filters-${bucket}`);
    if (!methods) {
      methods = document.createElement("div");
      methods.id = `method-filters-${bucket}`;
      methods.className = "source-filters method-filters";
      methods.setAttribute("aria-label", "Filter jobs by application method");
      filters.insertAdjacentElement("afterend", methods);
    }
    const sourceJobs = jobs.filter(job => selected === "all" || job.source_mode === selected);
    methods.innerHTML = [["all", "All"], ["easy_apply", "Easy Apply"], ["external", "External"]].map(([value, label]) => {
      const count = value === "all" ? sourceJobs.length : sourceJobs.filter(job => job.application_method === value).length;
      return `<button class="btn-sm" aria-pressed="${method === value}" onclick="selectJobMethod('${bucket}', '${value}')">${label} (${count})</button>`;
    }).join("");
    const visible = visibleJobs(bucket);
    if (bucket !== "applied") {
      let actions = document.getElementById(`delete-controls-${bucket}`);
      if (!actions) {
        actions = document.createElement("div");
        actions.id = `delete-controls-${bucket}`;
        actions.className = "job-delete-controls";
        methods.insertAdjacentElement("afterend", actions);
      }
      const deletable = visible.filter(job => !job.deletion_protected);
      const selectedCount = deletable.filter(job => selectedJobs.has(job.job_url)).length;
      actions.innerHTML = `<span>${visible.length} shown</span>
        <button class="btn-sm" ${deletingJobs || !selectedCount ? "disabled" : ""} onclick="deleteJobs('${bucket}', 'selected')">Delete selected (${selectedCount})</button>
        <button class="btn-sm" ${deletingJobs || !deletable.length ? "disabled" : ""} onclick="deleteJobs('${bucket}', 'filtered')">Clear current filtered list (${deletable.length})</button>
        ${deletable.length < visible.length ? '<span>Applied history and active applications are protected.</span>' : ''}`;
    }
    document.getElementById(containerId).innerHTML = visible.length
      ? visible.map(renderJobCard).join("")
      : '<div class="empty-state">No jobs in this view.</div>';
  }
  document.getElementById("stat-review-count").innerText = dashboardJobs.filter(j => jobBucket(j) === "review").length;
  document.getElementById("stat-applied-count").innerText = dashboardJobs.filter(j => jobBucket(j) === "applied").length;
  document.getElementById("stat-flagged-count").innerText = dashboardJobs.filter(j => jobBucket(j) === "failed").length;
}

function renderJobCard(job) {
  const review = job.review;
  const saved = ["SAVED_FOR_REVIEW", "POSSIBLE_MATCH"].includes(job.status_before_manual || job.status);
  return `<div class="flagged-card review-card ${saved ? "saved-job-card" : ""} ${["skipped", "failed"].includes(jobBucket(job)) ? "search-issue-card" : ""}">
    <h3>${escapeHtml(job.job_title || "Title unavailable")} — ${escapeHtml(job.company || "Company unavailable")}
      <span class="source-badge">${{search_save: "SEARCH", autopilot: "AUTO"}[job.source_mode] || "LEGACY"}</span>
      ${["easy_apply", "external"].includes(job.application_method) ? `<span class="application-method-badge">${job.application_method === "easy_apply" ? "EASY APPLY" : "EXTERNAL"}</span>` : ""}</h3>
    ${jobStatusControl(job)}
    ${jobBucket(job) !== "applied" && !job.deletion_protected ? `<div class="job-delete-controls">
      <label><input type="checkbox" aria-label="Select job for deletion" data-job-url="${escapeHtml(job.job_url)}"
        ${selectedJobs.has(job.job_url) ? "checked" : ""} ${deletingJobs ? "disabled" : ""} onchange="selectJobForDeletion(this)"> Select</label>
      <button class="btn-sm" data-job-url="${escapeHtml(job.job_url)}" ${deletingJobs ? "disabled" : ""}
        onclick="deleteJobs('${jobBucket(job)}', 'single', this.dataset.jobUrl)">Delete job</button></div>` : ""}
    ${saved ? `<p><strong>Status:</strong> ${(job.status_before_manual || job.status) === "POSSIBLE_MATCH" ? "Possible Match" : "Saved for Review"} — ${job.tracking_status === "Pending" ? "Not Applied" : job.tracking_status}</p>` : ""}
    <p><strong>Location:</strong> ${escapeHtml(job.location || "Not listed")}</p>
    <p><strong>Job type:</strong> ${escapeHtml(job.employment_type || job.job_type || "Not listed")}</p>
    <p><strong>Workplace:</strong> ${escapeHtml(job.workplace_type || "Not listed")}</p>
    <p><strong>Fit score:</strong> ${job.match_score == null ? "Not scored" : Number(job.match_score) + "/100"}</p>
    <p><strong>LinkedIn Job ID:</strong> ${escapeHtml(job.external_job_id || "Not available")}</p>
    ${job.failure_reason || job.reason || job.reason_flagged ? `<p><strong>Reason:</strong> ${escapeHtml(job.failure_reason || job.reason_flagged || job.reason)}</p>` : ""}
    ${job.error ? `<p><strong>Details:</strong> ${escapeHtml(job.error)}</p>` : ""}
    ${job.search_query ? `<p><strong>Search query:</strong> ${escapeHtml(job.search_query)}</p>` : ""}
    ${job.date_applied ? `<p><strong>Date applied:</strong> ${escapeHtml(job.date_applied)}</p>` : ""}
    <details><summary>Job description and match details</summary>
      <p style="white-space: pre-wrap">${escapeHtml(job.description)}</p>
      <p style="white-space: pre-wrap">${escapeHtml(job.notes)}</p>
      <p>Workflow: ${escapeHtml(job.status)} · Original bucket: ${escapeHtml(job.original_bucket)}</p>
      <p>${escapeHtml(job.date_discovered || job.occurred_at || job.processed_at)}</p>
    </details>
    <p><a href="${escapeHtml(job.job_url)}" target="_blank" rel="noopener">View job posting ↗</a></p>
    ${job.application_url ? `<p><a href="${escapeHtml(job.application_url)}" target="_blank" rel="noopener">Open external application</a></p>` : ""}
    ${review ? `<p><strong>System:</strong> ${escapeHtml(review.application_system || job.ats_type || "Unknown")}</p>
      <p><strong>Required fields complete:</strong> ${review.required_fields_complete ? "Yes" : "No"}</p>
      <p><strong>Resume uploaded:</strong> ${review.resume_uploaded ? "Yes (" + escapeHtml(review.resume_used || "configured resume") + ")" : "No / not requested"}</p>
      <p><strong>Final Submit detected:</strong> ${review.final_submit_detected ? "Yes" : "No"}</p>
      <p><strong>Application steps:</strong> ${review.step_count == null ? "Unknown" : Number(review.step_count)}</p>
      ${jobBucket(job) === "review" && review.status === "awaiting_approval" ? `<div class="review-actions">
        <button class="btn btn-primary" onclick="decideApplicationReview(${Number(review.id)}, 'approve')">Approve & Submit</button>
        <button class="btn btn-danger" onclick="decideApplicationReview(${Number(review.id)}, 'reject')">Reject</button>
      </div>` : ""}` : ""}
    ${jobBucket(job) === "failed" && job.flagged_id ? (Array.isArray(job.unhandled_questions) ? job.unhandled_questions : []).map((q, index) => `<div class="form-group mt-4">
      <label><strong>Question:</strong> ${escapeHtml(q.question)}</label>
      <input type="text" id="teach-input-${job.flagged_id}-${index}" placeholder="Enter answer to teach memory store...">
      <button class="btn btn-primary btn-sm mt-4" data-question="${escapeHtml(q.question)}" onclick="teachQuestion(${Number(job.flagged_id)}, this.dataset.question, ${index})">Save Answer & Resolve</button>
    </div>`).join("") : ""}
  </div>`;
}

const loadApplicationReviews = loadDashboardJobs;
const loadSearchIssues = loadDashboardJobs;
const loadAppliedJobs = loadDashboardJobs;
const loadFlaggedJobs = loadDashboardJobs;

async function decideApplicationReview(reviewId, decision) {
  const action = decision === "approve" ? "submit" : "reject";
  if (!window.confirm(`Are you sure you want to ${action} this application?`)) {
    return;
  }
  try {
    const res = await fetch(`/api/applications/reviews/${reviewId}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "The review decision could not be saved.");
    }
    appendLog(`Application review #${reviewId} ${data.decision}.`, "system");
    await loadApplicationReviews();
  } catch (err) {
    alert(`Review error: ${err.message || err}`);
    await loadApplicationReviews();
  }
}

async function teachQuestion(flaggedId, questionText, index = 0) {
  const inputElem = document.getElementById(`teach-input-${flaggedId}-${index}`);
  const answer = inputElem ? inputElem.value : "";

  if (!answer.trim()) {
    alert("Please enter an answer to teach the software!");
    return;
  }

  try {
    const res = await fetch("/api/jobs/teach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flagged_id: flaggedId, question: questionText, answer: answer })
    });
    if (res.ok) {
      appendLog(`💡 Taught software answer for: '${questionText}' -> '${answer}'`, "system");
      await loadFlaggedJobs();
    }
  } catch (err) {
    alert(`Error: ${err}`);
  }
}

// Start Autopilot Trigger
async function startSearchAndSave() {
  const btn = document.getElementById("btn-search-save");
  btn.disabled = true;
  try {
    const res = await fetch("/api/search/start", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Search could not start.");
    appendLog(data.message, "system");
  } catch (err) {
    appendLog(`Search & Save failed: ${err.message || err}`, "system");
  } finally {
    await loadSearchStatus();
  }
}

async function loadSearchStatus() {
  const btn = document.getElementById("btn-search-save");
  try {
    const res = await fetch("/api/search/status");
    if (!res.ok) throw new Error("Search status unavailable");
    const status = await res.json();
    const wasRunning = btn.dataset.running === "true";
    btn.dataset.running = String(status.running);
    btn.disabled = status.running;
    btn.innerText = status.running ? "Searching & Saving..." : "Search & Save Jobs";
    if (status.running || wasRunning) await loadSearchIssues();
    if (wasRunning && !status.running) await loadApplicationReviews();
  } catch (err) {
    btn.disabled = false;
    btn.innerText = "Search & Save Jobs";
    console.error(err);
  }
}

async function startAutopilot() {
  const btn = document.getElementById("btn-start");
  btn.disabled = true;
  btn.innerText = "⏳ Autopilot Running...";
  appendLog("🚀 Requesting Autopilot run start...", "system");

  try {
    const res = await fetch("/api/autopilot/start", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Session could not start.");
    appendLog(`ℹ️ ${data.message}`, "system");
  } catch (err) {
    appendLog(`❌ Failed to start autopilot: ${err}`, "system");
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.innerText = "▶️ Start Autopilot";
    }, 5000);
  }
}

async function loadRuntimeStatus() {
  try {
    const res = await fetch("/api/runtime/status");
    const status = await res.json();
    document.getElementById("stat-current-job").innerText = status.current_job || "—";
    document.getElementById("stat-current-company").innerText = status.company || "—";
    document.getElementById("stat-current-ats").innerText = status.current_ats || "—";
    document.getElementById("stat-current-action").innerText = status.current_action || "Idle";
    document.getElementById("stat-runtime-reviewed").innerText = status.jobs_reviewed || 0;
    document.getElementById("stat-runtime-started").innerText = status.applications_started || 0;
    document.getElementById("stat-runtime-ready").innerText = status.ready_for_review || 0;
    document.getElementById("stat-runtime-failures").innerText = status.failures || 0;
  } catch (err) {
    console.error("Error loading runtime status:", err);
  }
}

// Download one of the two separate Excel reports. The backend refreshes both
// files together so they always represent the same database state.
function downloadExcel(reportType) {
  window.location.href = `/api/export/${reportType}`;
}

// Real-time Console Log Stream (WebSockets)
function initWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/logs`;
  const socket = new WebSocket(wsUrl);

  socket.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "log") {
        appendLog(msg.data);
        loadRuntimeStatus();
        const normalized = msg.data.toLowerCase();
        if (normalized.includes("search & save task finished")) loadSearchIssues();
        if (normalized.includes("approval required") || normalized.includes("review #") || normalized.includes("saved for review") || normalized.includes("search & save task finished")) {
          loadApplicationReviews();
        }
        // Refresh tables if application processing completed.
        if (normalized.includes("submitted") || normalized.includes("flagged")) {
          loadAppliedJobs();
          loadFlaggedJobs();
          loadApplicationReviews();
        }
      }
    } catch (e) {
      appendLog(event.data);
    }
  };

  socket.onclose = () => {
    setTimeout(initWebSocket, 3000);
  };
}

function appendLog(text, type = "normal") {
  const logOutput = document.getElementById("log-output");
  const div = document.createElement("div");
  div.className = `log-entry ${type}`;
  div.innerText = `[${new Date().toLocaleTimeString()}] ${text}`;
  logOutput.appendChild(div);
  logOutput.scrollTop = logOutput.scrollHeight;
}

function escapeHtml(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function escapeJs(str) {
  return (str || "").replace(/'/g, "\\'");
}
