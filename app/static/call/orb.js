export default class OrbController {
  constructor(orbElement) {
    this.el = orbElement;
    this.state = "idle";
  }

  setState(state) {
    this.state = state;
    this.el.dataset.state = state;
  }

  setLevel(level) {
    const clamped = Math.max(0, Math.min(1, level));
    this.el.style.setProperty("--orb-level", clamped.toFixed(3));
  }
}
