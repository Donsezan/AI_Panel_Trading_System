// The one thing on this page that needs scripting: telling an operator they are about to lose an
// edit. Everything else — the section rail, the Champion/Challenger strip, the seat master–detail
// and the row actions — works with scripting off, because the screen that edits risk limits must
// not depend on it. The tabs are `:checked` CSS over radios and the row buttons keep `formaction`
// beside their `hx-*`, so this file is enhancement and nothing here is load-bearing.
//
// Publish sits in a sticky bar now, but `add instrument` and `remove` still sit at eye level and
// still look like commits. An operator who edits a stop multiple, clicks `add fallback`, then
// navigates away used to lose the edit silently.
(function () {
  "use strict";

  //: Buttons that re-post the draft and publish nothing. They come back with the edit still
  //: unsaved, so the guard must stay armed across one.
  var DRAFT_BUTTONS = ["add", "remove", "lookup"];

  var dirty = false;

  // Delegated to the document rather than bound to the form element: an htmx swap replaces the
  // whole `#basket-form` node, and listeners bound to the old one are discarded with it — leaving
  // the page silently unguarded from the first row action onward.
  function inForm(target) {
    return target instanceof Element && target.closest("#basket-form") !== null;
  }

  document.addEventListener("input", function (event) {
    if (inForm(event.target)) dirty = true;
  });
  document.addEventListener("change", function (event) {
    if (inForm(event.target)) dirty = true;
  });

  // Publishing is the save; a row action is not. With htmx active a row action never reaches here
  // at all (it is an AJAX post, not a form submit), so this only matters when htmx is absent —
  // and there, clearing on `add instrument` would drop the guard for the rest of the edit.
  document.addEventListener("submit", function (event) {
    if (!inForm(event.target)) return;
    var submitter = event.submitter;
    if (!submitter || DRAFT_BUTTONS.indexOf(submitter.name) === -1) dirty = false;
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
})();
