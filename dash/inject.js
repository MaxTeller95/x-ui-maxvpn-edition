// Adds an "Optimizer" item to the x-ui sidebar (and the mobile drawer menu), just above
// Log Out. nginx inserts this script into the panel's pages; the panel itself is untouched.
(function () {
  var HREF = "__PREFIX__";
  var ICON = '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/></svg>';

  function addTo(menu) {
    if (menu.querySelector(".xo-dash-item")) return;
    var items = menu.querySelectorAll(":scope > li.ant-menu-item");
    if (items.length < 3) return;                       // not the main navigation menu
    var src = items[items.length - 2], last = items[items.length - 1];
    var li = src.cloneNode(true);
    li.classList.remove("ant-menu-item-selected", "ant-menu-item-active");
    li.classList.add("xo-dash-item");
    li.removeAttribute("aria-selected");
    var icon = li.querySelector(".anticon, svg");
    if (icon) { var s = document.createElement("span"); s.className = "anticon"; s.innerHTML = ICON; icon.replaceWith(s); }
    var label = li.querySelector("span:not(.anticon)") || li;
    label.textContent = "Optimizer";
    li.title = "Optimizer";
    li.addEventListener("click", function (e) { e.stopPropagation(); location.href = HREF; }, true);
    last.parentNode.insertBefore(li, last);
  }

  function scan() {
    document.querySelectorAll(".ant-layout-sider ul.ant-menu, .ant-drawer ul.ant-menu").forEach(addTo);
  }

  new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", scan);
  scan();
})();
