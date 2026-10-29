"""Versioned public rendered-page contracts; no browser state or API reads.

LEGACY_TOUR is the prior audited frontend. RAIL_INPUT_GROUP was checked against
public layout-7d2ae79b166fb789.js, page-359f07c7c4f25228.js,
8839-7616346476db11e7.js, 372-c46d9fe7ab2c7403.js, and the imported media,
message and Lucide components. The exact source hashes/anchors are retained in
this review's evidence. These selectors are not proof of a real logged-in page.
Only one recognized contract may be active. No fallback accepts an arbitrary
composer as authentication or a future unknown page as a known version.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Contract:
    name: str
    account: str
    composer: str
    send: str
    picker: str
    area: str


LEGACY_TOUR = Contract(
    'LEGACY_TOUR', "[data-tour='user-menu']",
    "[data-tour='chat-area'] textarea[name='input']",
    "[data-tour='chat-area'] > button:has(svg.lucide-arrow-up)",
    "input[type='file'][accept='image/*']",
    "xpath=ancestor::*[@data-tour='chat-area'][1]")
RAIL_INPUT_GROUP = Contract(
    'RAIL_INPUT_GROUP', "button[data-analytics-id='nav.rail.profile'][data-slot='sidebar-menu-button'][data-sidebar='menu-button']",
    "[data-slot='input-group'][role='group'] > textarea[data-slot='input-group-control'][name='input']",
    "[data-slot='input-group'][role='group']:has(> textarea[data-slot='input-group-control'][name='input']) > [data-slot='input-group-addon'][data-align='block-end'] > button:has(svg.lucide-arrow-up)",
    "input[type='file'][accept='image/*,.heic,.heif']",
    "xpath=ancestor::*[@data-slot='input-group'][1]")
LEGACY_SIGNIN = "[data-tour='sign-in']"
LEGACY_MARKERS = "[data-tour='user-menu'], [data-tour='sign-in'], [data-tour='chat-area']"
CURRENT_PROFILE_MARKER = "[data-analytics-id='nav.rail.profile']"
CURRENT_MARKERS = "[data-analytics-id='nav.rail.profile'], [data-slot='input-group']"
CURRENT_CHAT_NAV = "button[data-analytics-id='nav.chat']"
CURRENT_NEW_CHAT = "button[data-analytics-id='nav.chat.new']"
CURRENT_PREVIEWS = ":scope > [data-slot='attachment-group']"
CURRENT_ATTACHMENT = ":scope > [data-slot='attachment'][data-state='done'][data-orientation='vertical']"
CURRENT_PREVIEW_IMAGE = ":scope > [data-slot='attachment-media'][data-variant='image'] img"

# The current new-chat component mounts both copies of the same composer.
# Its two direct children are the desktop and mobile responsive branches;
# CSS displays exactly one at each breakpoint. These literal classes and the
# sibling structure come from eV() in page-359f07c7c4f25228.js, whose retained
# SHA-256 is e125cae602b6c8f6fb7090df7850a0ffb158047222440d22a0aa9fc1b4139f1f.
# This is a recognized two-copy layout, never a generic hidden-control bypass.
CURRENT_RESPONSIVE_ROOT = "div[class='flex flex-col w-full h-full']"
CURRENT_DESKTOP_BRANCH = "div[class='hidden md:flex flex-col w-full h-full']"
CURRENT_MOBILE_BRANCH = "div[class='flex flex-col h-full md:hidden']"


def _presentation_parent(composer):
    # The exact composer selector already requires its direct group parent.
    # Keep the original grandparent-role predicate on a single active locator.
    # Sending the first message can replace the responsive pair with one
    # composer between the count and this read. Do not spend the default 30s
    # waiting for a sibling that no longer exists. An unverified observation
    # lets the existing caller rescan within its existing operation deadline.
    from playwright.sync_api import TimeoutError as LocatorTimeoutError
    try:
        return composer.locator('xpath=..').locator('xpath=..').get_attribute('role', timeout=250) == 'presentation'
    except LocatorTimeoutError:
        return False


def _responsive_pair(page, composers):
    """Recognize only the public new-chat desktop/mobile sibling pair."""
    roots = page.locator(CURRENT_RESPONSIVE_ROOT
        + ':has(> ' + CURRENT_DESKTOP_BRANCH + ')'
        + ':has(> ' + CURRENT_MOBILE_BRANCH + ')')
    if roots.count() != 1:
        return False
    root = roots.nth(0)
    desktop = root.locator(':scope > ' + CURRENT_DESKTOP_BRANCH)
    mobile = root.locator(':scope > ' + CURRENT_MOBILE_BRANCH)
    if (root.locator(':scope > *').count() != 2
            or desktop.count() != 1 or mobile.count() != 1
            or desktop.locator(RAIL_INPUT_GROUP.composer).count() != 1
            or mobile.locator(RAIL_INPUT_GROUP.composer).count() != 1):
        return False
    return all(_presentation_parent(composers.nth(index)) for index in range(2))


def _current_composer_contract(page, composers, visible, count):
    total = count('current_composer', composers.count())
    if total not in (1, 2):
        return False
    active = visible(RAIL_INPUT_GROUP.composer)
    if (count('current_visible_composers', len(active)) != 1
            or count('global_composers', page.locator("textarea[name='input']").count()) != total):
        return False
    if total == 1:
        # The existing-chat layout has one composer. Preserve its exact path.
        return _presentation_parent(active[0])
    return _responsive_pair(page, composers)


def detect(page, visible, observation=None):
    """Return a known authenticated layout, a known anonymous state, or unknown.

    The profile rail button is rendered only after hydration, auth readiness and
    authenticated=true in the current public layout. Its absence is ambiguous:
    anonymous/loading/changed layouts must not be promoted to authenticated.
    """
    # These wrappers record only values the original decision already reads.
    # Returning each original value preserves its predicate and short circuit.
    def count(name, value):
        if observation is not None:
            observation['counts'][name] = min(value, 129)
        return value

    def reason(value):
        if observation is not None:
            observation['reason'] = value

    old_account = page.locator(LEGACY_TOUR.account)
    signin = page.locator(LEGACY_SIGNIN)
    current_account = page.locator(RAIL_INPUT_GROUP.account)
    current_composer = page.locator(RAIL_INPUT_GROUP.composer)
    current_seen = count('current_markers', page.locator(CURRENT_MARKERS).count())
    old_seen = count('legacy_markers', page.locator(LEGACY_MARKERS).count())
    if old_seen and current_seen:
        reason('LAYOUT_MIXED')
        return None, 'UNAVAILABLE'
    if old_seen:
        if count('legacy_signin', signin.count()) and not count('legacy_account', old_account.count()):
            reason('LEGACY_LOGIN_REQUIRED')
            return None, 'LOGIN_REQUIRED'
        if (count('legacy_account', old_account.count()) == 1 and count('legacy_signin', signin.count()) == 0
                and count('legacy_account_buttons', old_account.locator('button').count()) == 1
                and count('legacy_visible_composers', len(visible(LEGACY_TOUR.composer))) == 1):
            reason('CONNECTED')
            return LEGACY_TOUR, 'CONNECTED'
        reason('LEGACY_CONTRACT_UNVERIFIED')
        return None, 'UNAVAILABLE'
    if (count('current_profile_markers', page.locator(CURRENT_PROFILE_MARKER).count()) == 1
            and count('current_account', current_account.count()) == 1
            and count('current_visible_accounts', len(visible(RAIL_INPUT_GROUP.account))) == 1
            and _current_composer_contract(page, current_composer, visible, count)):
        reason('CONNECTED')
        return RAIL_INPUT_GROUP, 'CONNECTED'
    reason('CURRENT_CONTRACT_UNVERIFIED')
    return None, 'UNAVAILABLE'


CURRENT_ASSISTANT_TEXT = r'''(root) => {
    function render(node) {
        if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        if (node.matches('button, svg, script, style')) return '';
        if (node.matches('pre')) throw Error('UNRECOGNIZED_RENDERED_CODE');
        if (node.matches('[data-slot="card"]')) {
            const headers = node.querySelectorAll(':scope > [data-slot="card-header"]');
            const titles = node.querySelectorAll(':scope > [data-slot="card-header"] > [data-slot="card-title"]');
            const codes = node.querySelectorAll(':scope > div > code');
            if (headers.length !== 1 || titles.length !== 1 || codes.length !== 1
                    || node.querySelectorAll('code').length !== 1
                    || node.querySelector('pre, [data-slot="card"]')) {
                throw Error('UNRECOGNIZED_RENDERED_CODE');
            }
            const language = titles[0].textContent.trim();
            if (!/^[A-Za-z0-9_-]{1,64}$/.test(language)) throw Error('UNRECOGNIZED_RENDERED_CODE');
            const code = codes[0].cloneNode(true);
            // The source-generated line number is a span with this exact
            // class. Its text is display furniture, never answer content.
            code.querySelectorAll('span.react-syntax-highlighter-line-number').forEach(line => line.remove());
            if (code.querySelector('button, svg, script, style, pre, code, [data-slot="card"]')) {
                throw Error('UNRECOGNIZED_RENDERED_CODE');
            }
            return '\n```' + language + '\n' + code.textContent + '\n```\n';
        }
        if (!node.querySelector('[data-slot="card"]')) return node.innerText;
        return Array.from(node.childNodes).map(render).join('\n');
    }
    return Array.from(root.childNodes).map(render).join('\n\n').trim();
}'''
