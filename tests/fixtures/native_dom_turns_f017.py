"""Verbatim native reader/renderer excerpts, full DOM source f0172940be07f4126ac2fbcfb4c3f538da0c5d13d910af6e48b279ff3d6900d8."""
import re
class NotSubmitted(Exception): pass
RAIL_INPUT_GROUP=object()
ASSISTANT_BODY='div.text-foreground.max-w-none'
COMPLETE_ACTIONS=('button:has(svg.lucide-copy)', 'button:has(svg.lucide-thumbs-up)', 'button:has(svg.lucide-thumbs-down)')
MESSAGE='[data-message-id]'
USER_BODY='div.whitespace-pre-wrap.break-words'
CURRENT_ASSISTANT_TEXT='(root) => {\n    function render(node) {\n        if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;\n        if (node.nodeType !== Node.ELEMENT_NODE) return \'\';\n        if (node.matches(\'button, svg, script, style\')) return \'\';\n        if (node.matches(\'pre\')) throw Error(\'UNRECOGNIZED_RENDERED_CODE\');\n        if (node.matches(\'[data-slot="card"]\')) {\n            const headers = node.querySelectorAll(\':scope > [data-slot="card-header"]\');\n            const titles = node.querySelectorAll(\':scope > [data-slot="card-header"] > [data-slot="card-title"]\');\n            const codes = node.querySelectorAll(\':scope > div > code\');\n            if (headers.length !== 1 || titles.length !== 1 || codes.length !== 1\n                    || node.querySelectorAll(\'code\').length !== 1\n                    || node.querySelector(\'pre, [data-slot="card"]\')) {\n                throw Error(\'UNRECOGNIZED_RENDERED_CODE\');\n            }\n            const language = titles[0].textContent.trim();\n            if (!/^[A-Za-z0-9_-]{1,64}$/.test(language)) throw Error(\'UNRECOGNIZED_RENDERED_CODE\');\n            const code = codes[0].cloneNode(true);\n            // The source-generated line number is a span with this exact\n            // class. Its text is display furniture, never answer content.\n            code.querySelectorAll(\'span.react-syntax-highlighter-line-number\').forEach(line => line.remove());\n            if (code.querySelector(\'button, svg, script, style, pre, code, [data-slot="card"]\')) {\n                throw Error(\'UNRECOGNIZED_RENDERED_CODE\');\n            }\n            return \'\\n```\' + language + \'\\n\' + code.textContent + \'\\n```\\n\';\n        }\n        if (!node.querySelector(\'[data-slot="card"]\')) return node.innerText;\n        return Array.from(node.childNodes).map(render).join(\'\\n\');\n    }\n    return Array.from(root.childNodes).map(render).join(\'\\n\\n\').trim();\n}'

def _native_turns(self):
        result, identifiers = [], set()
        for turn in self._visible(MESSAGE):
            identifier = turn.get_attribute('data-message-id')
            if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', identifier)
                    or identifier in identifiers):
                raise NotSubmitted('UNAVAILABLE')
            identifiers.add(identifier)
            user = turn.locator(USER_BODY)
            answer = turn.locator(ASSISTANT_BODY)
            if user.count() == 1 and answer.count() == 0:
                # The user bubble may visually collapse a long prompt, but its
                # text node retains the complete prompt. Generic Uploaded media
                # labels are not filenames; do not invent filename proof here.
                images = turn.locator('img')
                accepted = images.count() == len(self.uploaded_names)
                for index in range(images.count()):
                    image = images.nth(index)
                    accepted = accepted and image.is_visible() and image.evaluate(
                        '(image) => image.complete && image.naturalWidth > 0 && image.naturalHeight > 0')
                    label = image.get_attribute('alt') or ''
                    expected = (self.uploaded_names[index] if self._dom_contract == RAIL_INPUT_GROUP
                        and index < len(self.uploaded_names) else 'Uploaded media ' + str(index + 1))
                    accepted = accepted and label == expected
                result.append(dict(role='user', text=user.text_content() or '',
                    attachments=list(self.uploaded_names) if accepted else ['UNCONFIRMED']))
            elif user.count() == 0 and answer.count() == 1:
                footer = answer.locator('xpath=..').locator(
                    ":scope > div.flex.items-center[class~='-mx-1.5'] > div.flex.items-center[class~='gap-0.5']")
                complete = footer.count() == 1 and all(
                    footer.locator(selector).count() == 1 for selector in COMPLETE_ACTIONS)
                # The public message component renders these three footer
                # actions only after streaming finishes in a non-shared chat.
                text = self._assistant_text(answer)
                result.append(dict(role='assistant', text=text, complete=complete, id=identifier))
            else:
                # An assistant placeholder may precede answer content. It is
                # incomplete, never an executable answer or evidence of a user.
                result.append(dict(role='unknown', text='', complete=False))
        return result

def _native_assistant_text(self, body):
        if self._dom_contract == RAIL_INPUT_GROUP:
            return body.evaluate(CURRENT_ASSISTANT_TEXT, timeout=self._timeout(10000))
        # The page renders Markdown fences as <pre><code>. Reconstruct those
        # visible code blocks, excluding their UI Copy/language controls, while
        # retaining every rendered prose block. No framework state is read.
        return body.evaluate('''(root) => {
            function render(node) {
                if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
                if (node.nodeType !== Node.ELEMENT_NODE) return '';
                if (node.matches('button, svg, script, style')) return '';
                if (node.matches('pre')) {
                    const code = node.querySelector('code');
                    if (!code) return node.innerText;
                    const language = /(?:^|\\s)language-([A-Za-z0-9_-]+)/.exec(code.className);
                    return '\\n```' + (language ? language[1] : '') + '\\n'
                        + code.textContent + '\\n```\\n';
                }
                // Nested prose around a code block is still part of the
                // answer. Never replace its whole enclosing container.
                if (!node.querySelector('pre')) return node.innerText;
                return Array.from(node.childNodes).map(render).join('\\n');
            }
            return Array.from(root.childNodes).map(render).join('\\n\\n').trim();
        }''', timeout=self._timeout(10000))

class NativeReference:
    def __init__(self, page, names=()):
        self.page=page; self.uploaded_names=list(names); self._dom_contract=RAIL_INPUT_GROUP
    def _visible(self, selector):
        nodes=self.page.locator(selector)
        return [nodes.nth(index) for index in range(nodes.count()) if nodes.nth(index).is_visible()]
    def _timeout(self, timeout): return timeout
    def _fresh_conversation(self): return None
    _turns=_native_turns
    _assistant_text=_native_assistant_text
