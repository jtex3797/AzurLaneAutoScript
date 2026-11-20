// Fork: War Archives picker, loaded by module/webui/fork_war_archives.py inject().
// Widens the WarArchives event dropdown, adds an info line and a searchable side panel.
// window.forkWarArchives = {archive id: [Japanese, Korean, English]} is set right before this file.
(function () {
    if (window.__forkWaInit) {
        return;
    }
    window.__forkWaInit = true;

    var NAME = 'WarArchives_Campaign_Event';
    var SCOPE = 'pywebio-scope-arg_container-select-' + NAME;
    var panel = null;
    var list = null;
    var search = null;

    // pywebio keeps option values JSON-encoded ("\"war_archives_...\""), the raw value is what select.value takes
    function archiveId(value) {
        try {
            var parsed = JSON.parse(value);
            return typeof parsed === 'string' ? parsed : value;
        } catch (e) {
            return value;
        }
    }

    function data(value) {
        var all = window.forkWarArchives || {};
        return all[archiveId(value)] || null;
    }

    function month(id) {
        var m = /(\d{4})(\d{2})\d{2}/.exec(id);
        return m ? m[1] + '-' + m[2] : '';
    }

    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
        });
    }

    function koHtml(d) {
        return d && d[1]
            ? '<span class="fork-wa-ko">' + esc(d[1]) + '</span>'
            : '<span class="fork-wa-miss">한국명 미수집</span>';
    }

    function findSelect() {
        return document.querySelector('select[name="' + NAME + '"]');
    }

    function infoHtml(id) {
        var d = data(id);
        if (!d) {
            return esc(archiveId(id));
        }
        return '<b class="fork-wa-ja">' + esc(d[0] || '-') + '</b>'
            + '<span class="fork-wa-sep">|</span>' + koHtml(d)
            + '<span class="fork-wa-sep">|</span><span>' + esc(d[2]) + '</span>'
            + '<span class="fork-wa-date">' + month(id) + '</span>';
    }

    function decorate(select) {
        if (select.getAttribute('data-fork-wa')) {
            return;
        }
        select.setAttribute('data-fork-wa', '1');
        var scope = document.getElementById(SCOPE);
        if (scope) {
            scope.classList.add('fork-wa-wide');
        }

        var box = document.createElement('div');
        box.className = 'fork-wa-box';
        var info = document.createElement('div');
        info.className = 'fork-wa-info';
        info.innerHTML = infoHtml(select.value);
        if (!select.disabled) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'fork-wa-btn';
            btn.textContent = '목록에서 고르기';
            btn.addEventListener('click', openPanel);
            box.appendChild(btn);
        }
        box.appendChild(info);

        select.addEventListener('change', function () {
            info.innerHTML = infoHtml(select.value);
            info.classList.add('fork-wa-flash');
            setTimeout(function () {
                info.classList.remove('fork-wa-flash');
            }, 400);
        });

        // Below the select's own wrapper, as a new row of the arg container grid
        var anchor = select.parentElement && select.parentElement !== scope ? select.parentElement : select;
        anchor.insertAdjacentElement('afterend', box);
    }

    function ensurePanel() {
        if (panel) {
            return;
        }
        panel = document.createElement('aside');
        panel.className = 'fork-wa-panel';
        panel.setAttribute('aria-label', '작전문서 목록');
        panel.innerHTML = '<div class="fork-wa-head"><b>작전문서 목록<span class="fork-wa-count"></span></b>'
            + '<button type="button" class="fork-wa-close">닫기</button></div>'
            + '<input class="fork-wa-search" type="search" placeholder="일본어, 한국어, 영어 이름으로 검색">'
            + '<div class="fork-wa-list"></div>';
        document.body.appendChild(panel);
        list = panel.querySelector('.fork-wa-list');
        search = panel.querySelector('.fork-wa-search');
        panel.querySelector('.fork-wa-close').addEventListener('click', closePanel);
        search.addEventListener('input', render);
        list.addEventListener('click', function (e) {
            var item = e.target.closest('.fork-wa-item');
            if (item) {
                pick(item.getAttribute('data-id'));
            }
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                closePanel();
            }
        });
    }

    function render() {
        var select = findSelect();
        if (!select) {
            closePanel();
            return;
        }
        var term = search.value.trim().toLowerCase();
        var rows = [];
        // Options of the dropdown itself, so the server's own list and order apply
        Array.prototype.forEach.call(select.options, function (o) {
            var d = data(o.value) || ['', '', o.textContent];
            if (term && !d.some(function (s) {
                return s && s.toLowerCase().indexOf(term) >= 0;
            })) {
                return;
            }
            rows.push('<button type="button" class="fork-wa-item' + (o.value === select.value ? ' fork-wa-sel' : '')
                + '" data-id="' + esc(o.value) + '">'
                + '<span class="fork-wa-ja">' + esc(d[0] || d[2]) + '</span>'
                + '<span class="fork-wa-sub">' + koHtml(d) + '<span>' + esc(d[2]) + '</span>'
                + '<span class="fork-wa-date">' + month(o.value) + '</span></span></button>');
        });
        list.innerHTML = rows.length ? rows.join('') : '<div class="fork-wa-empty">검색 결과 없음</div>';
        panel.querySelector('.fork-wa-count').textContent = ' ' + rows.length + '개';
    }

    function openPanel() {
        ensurePanel();
        search.value = '';
        render();
        panel.classList.add('fork-wa-open');
        var sel = list.querySelector('.fork-wa-sel');
        if (sel) {
            sel.scrollIntoView({block: 'center'});
        }
        setTimeout(function () {
            search.focus();
        }, 50);
    }

    function closePanel() {
        if (panel) {
            panel.classList.remove('fork-wa-open');
        }
    }

    function pick(id) {
        // Look it up again: switching task or redrawing the group replaces the select
        var select = findSelect();
        if (!select) {
            closePanel();
            return;
        }
        select.value = id;
        select.dispatchEvent(new Event('input', {bubbles: true}));
        select.dispatchEvent(new Event('change', {bubbles: true}));
        closePanel();
    }

    function scan() {
        var select = findSelect();
        if (select) {
            decorate(select);
        } else {
            closePanel();
        }
    }

    new MutationObserver(scan).observe(document.body, {childList: true, subtree: true});
    scan();
})();
