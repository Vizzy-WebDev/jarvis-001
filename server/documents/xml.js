// A small, forgiving XML parser — NOT a general XML parser. Office XML is
// machine-generated, always well-formed, and never has DTDs/entity tricks
// worth handling generically, so a hand-rolled tree builder is enough and
// avoids a dependency.
//
// parseXml() builds a lightweight tree (tag name namespace-stripped, attrs
// as a plain object, children in document order, direct text collected on
// the node) — not a real DOM, just enough structure for the format readers
// above it (docx.js/xlsx.js/pptx.js) to walk paragraphs/runs/rows/cells,
// which is genuinely nested (a table cell holds paragraphs holding runs)
// and needs more than a flat scan.
//
// A leaf module: no imports.

const ENTITY_MAP = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };

/** Decodes XML entities, including numeric ones (&#39; / &#x27;). Office XML uses both. */
export function decodeEntities(str) {
  if (!str || str.indexOf('&') === -1) return str || '';
  return str.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (whole, body) => {
    if (body[0] === '#') {
      const code = body[1] === 'x' || body[1] === 'X' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : whole;
    }
    return ENTITY_MAP[body] ?? whole;
  });
}

function stripNamespace(name) {
  const colon = name.indexOf(':');
  return colon === -1 ? name : name.slice(colon + 1);
}

const ATTR_RE = /([a-zA-Z_:][\w.:-]*)\s*=\s*"([^"]*)"|([a-zA-Z_:][\w.:-]*)\s*=\s*'([^']*)'/g;

function parseOpenTag(body) {
  const spaceIdx = body.search(/\s/);
  const rawName = spaceIdx === -1 ? body : body.slice(0, spaceIdx);
  const name = stripNamespace(rawName.trim());
  const attrs = {};
  if (spaceIdx !== -1) {
    const attrStr = body.slice(spaceIdx);
    let m;
    ATTR_RE.lastIndex = 0;
    while ((m = ATTR_RE.exec(attrStr))) {
      const key = stripNamespace(m[1] || m[3]);
      const val = decodeEntities(m[2] !== undefined ? m[2] : m[4]);
      attrs[key] = val;
    }
  }
  return { name, attrs };
}

/**
 * Parses an XML string into a lightweight tree and returns its root
 * (synthetic `#root`, so a document with stray top-level whitespace or a
 * single real root element are both handled the same way). Each node is
 * `{name, attrs, children: [], text}` — `text` is direct text content only
 * (concatenated); use `textContent()` for the recursive version.
 */
export function parseXml(xmlStr) {
  const root = { name: '#root', attrs: {}, children: [], text: '' };
  const stack = [root];
  const len = xmlStr.length;
  let i = 0;

  while (i < len) {
    const lt = xmlStr.indexOf('<', i);
    if (lt === -1) {
      if (i < len) appendText(xmlStr.slice(i));
      break;
    }
    if (lt > i) appendText(xmlStr.slice(i, lt));

    if (xmlStr.startsWith('<!--', lt)) {
      const end = xmlStr.indexOf('-->', lt + 4);
      i = end === -1 ? len : end + 3;
      continue;
    }
    if (xmlStr.startsWith('<![CDATA[', lt)) {
      const end = xmlStr.indexOf(']]>', lt + 9);
      appendText(xmlStr.slice(lt + 9, end === -1 ? len : end));
      i = end === -1 ? len : end + 3;
      continue;
    }
    if (xmlStr.startsWith('<?', lt)) {
      const end = xmlStr.indexOf('?>', lt + 2);
      i = end === -1 ? len : end + 2;
      continue;
    }
    if (xmlStr.startsWith('<!', lt)) {
      const end = xmlStr.indexOf('>', lt + 2);
      i = end === -1 ? len : end + 1;
      continue;
    }

    const gt = xmlStr.indexOf('>', lt + 1);
    if (gt === -1) {
      i = len;
      break;
    }
    const tag = xmlStr.slice(lt + 1, gt);
    i = gt + 1;

    if (tag[0] === '/') {
      // Malformed/mismatched close tags are ignored rather than thrown —
      // this is reached from user-uploaded files and a best-effort partial
      // read beats a hard failure on one bad tag.
      if (stack.length > 1) stack.pop();
      continue;
    }

    const selfClosing = tag.endsWith('/');
    const body = selfClosing ? tag.slice(0, -1) : tag;
    const { name, attrs } = parseOpenTag(body);
    const node = { name, attrs, children: [], text: '' };
    stack[stack.length - 1].children.push(node);
    if (!selfClosing) stack.push(node);
  }

  function appendText(raw) {
    const decoded = decodeEntities(raw);
    if (decoded.trim() || decoded.includes(' ')) stack[stack.length - 1].text += decoded;
  }

  return root;
}

/** Every descendant (not just direct children) whose namespace-stripped tag name matches, in document order. */
export function findAll(node, name, out = []) {
  for (const child of node.children) {
    if (child.name === name) out.push(child);
    findAll(child, name, out);
  }
  return out;
}

/** Direct children only whose tag name matches. */
export function findChildren(node, name) {
  return node.children.filter((c) => c.name === name);
}

/** The first direct child matching, or null. */
export function findChild(node, name) {
  return node.children.find((c) => c.name === name) || null;
}

/** All text in this node and every descendant, concatenated in document order — for pulling "all the words" out of a run/paragraph/cell regardless of how deeply nested. */
export function textContent(node) {
  let out = node.text || '';
  for (const child of node.children) out += textContent(child);
  return out;
}
