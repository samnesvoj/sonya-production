import './styles.css';
import { SonyaCloth } from './SonyaCloth.js';

export { SonyaCloth };

// Convenience auto-mount for the standalone preview.html: production
// integration (see integration-example.js) will call SonyaCloth.mount()
// explicitly instead of relying on this element id.
const root = document.getElementById('sonya-cloth-root');
if (root) {
  SonyaCloth.mount(root, {});
}
