import { useEffect, useRef } from 'react';

// 这是实时计算的动效，不使用录屏片段或原作者的图像素材。
// GPU计算玻璃球的珠光、边缘和流动波带；后端和LLM不参与。
const vertexSource = `
  attribute vec2 position;
  varying vec2 uv;
  void main() {
    uv = position;
    gl_Position = vec4(position, 0.0, 1.0);
  }
`;

const fragmentSource = `
  precision highp float;
  varying vec2 uv;
  uniform float time;
  uniform float teaTint;

  float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
  }
  float glow(vec2 p, vec2 center, vec2 radius) {
    vec2 q = (p - center) / radius;
    return exp(-dot(q, q) * 2.0);
  }
  void main() {
    float phase = time * 0.65;
    vec2 p = uv / 0.88;
    float r = length(p);
    float mask = 1.0 - smoothstep(0.985, 1.01, r);
    float z = sqrt(max(0.0, 1.0 - dot(p, p)));

    // 固定的球形边界；颜色和波带在球内流动，而不是整个入口来回移动。
    vec3 pearl = vec3(0.94, 0.96, 0.98);
    vec3 cyan = vec3(0.35, 0.92, 0.96);
    vec3 pink = vec3(0.97, 0.48, 0.90);
    vec3 violet = vec3(0.58, 0.39, 0.98);
    vec3 color = pearl;
    color = mix(color, cyan, 0.55 * glow(p, vec2(0.56, 0.12), vec2(0.85, 1.25)));
    color = mix(color, pink, 0.82 * glow(p, vec2(-0.30, -0.33), vec2(0.84, 0.87)));
    color = mix(color, violet, 0.46 * glow(p, vec2(0.26 * sin(phase), 0.43), vec2(0.70, 0.62)));
    color = mix(color, vec3(1.0, 0.93, 0.81), 0.25 * glow(p, vec2(-0.40, 0.45), vec2(0.60, 0.60)));

    // 两条有厚度的曲面波带：中心线决定起伏，边缘亮线表现玻璃折射感。
    float wave = 0.10 * sin(phase) + 0.17 * sin(p.x * 2.8 + phase)
      + 0.075 * sin(p.x * 4.0 - phase * 0.8);
    float upper = wave + 0.17 * z;
    float lower = wave - 0.12 * z - 0.06;
    float ribbonA = exp(-pow((p.y - upper) / 0.10, 2.0));
    float ribbonB = 1.0 - smoothstep(0.055, 0.11, abs(p.y - lower));
    float depth = 1.0 - smoothstep(0.85, 1.0, r);
    vec3 ribbonColor = mix(violet, cyan, smoothstep(-0.65, 0.65, p.x));
    color = mix(color, ribbonColor, ribbonA * 0.85 * depth);
    vec3 silk = mix(vec3(1.0, 0.86, 0.89), vec3(0.65, 0.98, 0.96), p.x * 0.5 + 0.5);
    color = mix(color, silk, ribbonB * 0.88 * depth);
    float edge = exp(-pow((p.y - lower - 0.07 * z) / 0.018, 2.0));
    color = mix(color, vec3(1.0), edge * 0.48 * depth);

    // 球面边缘和左上高光加强体积感；轮廓外保持透明以适应网站背景。
    float rim = pow(1.0 - z, 3.0);
    color = mix(color, vec3(0.87, 1.0, 0.99), rim * 0.85);
    float highlight = glow(p, vec2(-0.36, 0.54), vec2(0.72, 0.38));
    color = mix(color, vec3(1.0), highlight * 0.52);
    color += (hash(gl_FragCoord.xy) - 0.5) * 0.009;
    // 与画布的预乘Alpha约定一致；透明区域RGB也归零，避免合成时带出底色。
    float alpha = mask * 0.96;
    gl_FragColor = vec4(clamp(color, 0.0, 1.0) * alpha, alpha);
  }
`;

function compileShader(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader);
    throw new Error('Orb shader could not compile');
  }
  return shader;
}

function createPainter(canvas) {
  const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: false });
  if (!gl) {
    // 不支持WebGL时仍保留可点击入口；这是静态降级，不假装动效已正常运行。
    canvas.dataset.renderer = 'unavailable';
    return null;
  }
  const program = gl.createProgram();
  const vertex = compileShader(gl, gl.VERTEX_SHADER, vertexSource);
  const fragment = compileShader(gl, gl.FRAGMENT_SHADER, fragmentSource);
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('Orb could not link');
  gl.useProgram(program);
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const position = gl.getAttribLocation(program, 'position');
  gl.enableVertexAttribArray(position);
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
  const timeLocation = gl.getUniformLocation(program, 'time');
  const tintLocation = gl.getUniformLocation(program, 'teaTint');
  canvas.dataset.renderer = 'webgl';
  let frames = 0;
  return {
    draw(time, tint) {
      // 限制画布像素数，悬浮按钮不需要按整个屏幕的分辨率渲染。
      const size = Math.min(640, Math.round(canvas.clientWidth * Math.min(devicePixelRatio || 1, 2)));
      if (canvas.width !== size || canvas.height !== size) {
        canvas.width = size;
        canvas.height = size;
        gl.viewport(0, 0, size, size);
      }
      gl.uniform1f(timeLocation, time);
      gl.uniform1f(tintLocation, tint === 'tea' ? 1 : 0);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      frames += 1;
      canvas.dataset.frames = String(frames);
    },
    close() {
      gl.deleteBuffer(buffer);
      gl.deleteProgram(program);
      gl.deleteShader(vertex);
      gl.deleteShader(fragment);
    },
  };
}

export function Orb({ tint = 'mono', paused = false, thinking = false, className = '' }) {
  const canvasRef = useRef(null);
  const settingsRef = useRef({ tint, paused, thinking });
  const controllerRef = useRef(null);

  useEffect(() => {
    settingsRef.current = { tint, paused, thinking };
    controllerRef.current?.refresh();
  }, [tint, paused, thinking]);

  useEffect(() => {
    const canvas = canvasRef.current;
    let painter;
    try { painter = createPainter(canvas); } catch { canvas.dataset.renderer = 'unavailable'; }
    if (!painter) return;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let frameId = 0;
    let seconds = 2.2;
    let previous = 0;
    let visible = true;
    let lastPaint = 0;

    function animate(now) {
      frameId = 0;
      if (!visible || document.hidden || settingsRef.current.paused || reduced.matches) return;
      if (previous) seconds += Math.min((now - previous) / 1000, 0.05) * (settingsRef.current.thinking ? 1.4 : 1);
      previous = now;
      if (now - lastPaint >= 1000 / 30) {
        painter.draw(seconds, settingsRef.current.tint);
        lastPaint = now;
      }
      frameId = requestAnimationFrame(animate);
    }
    function refresh() {
      cancelAnimationFrame(frameId);
      previous = 0;
      painter.draw(seconds, settingsRef.current.tint);
      if (visible && !document.hidden && !settingsRef.current.paused && !reduced.matches) {
        frameId = requestAnimationFrame(animate);
      }
    }
    controllerRef.current = { refresh };
    const observer = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; refresh(); });
    observer.observe(canvas);
    const resizeObserver = new ResizeObserver(refresh);
    resizeObserver.observe(canvas);
    document.addEventListener('visibilitychange', refresh);
    reduced.addEventListener('change', refresh);
    refresh();
    return () => {
      cancelAnimationFrame(frameId);
      observer.disconnect();
      resizeObserver.disconnect();
      document.removeEventListener('visibilitychange', refresh);
      reduced.removeEventListener('change', refresh);
      painter.close();
      controllerRef.current = null;
    };
  }, []);

  return <canvas ref={canvasRef} className={`orb-canvas ${className}`} aria-hidden="true" />;
}
