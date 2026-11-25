// cursor_overlay.js — inject qua `context.add_init_script(path=...)` TRƯỚC KHI mở trang
// đầu tiên. Chạy lại tự động ở mọi navigation mới trong cùng context (add_init_script
// đăng ký cho cả context, không phải 1 lần cho 1 trang).
//
// Vẽ con trỏ MŨI TÊN (SVG) theo dõi sự kiện `mousemove` THẬT (Playwright page.mouse.move() dispatch
// native trusted input event) — tự động khít với mọi easing/smooth-move ở click_helpers.py
// mà không cần 2 script chia sẻ state — và một vòng gợn sóng ngắn ở mỗi `mousedown` để người xem
// thấy chỗ vừa nhấn. Xem REFERENCE.md §4.
//
// ĐẦU MŨI TÊN (path điểm 0,0 = góc trên-trái của khối) nằm ĐÚNG toạ độ chuột: click_helpers.py tính
// mọi vị trí theo điểm đó, không theo hộp bao của hình.
(function () {
  var ARROW_SCALE = 1.5;                 // phóng to mũi tên 16x23 (px CSS) cho dễ thấy ở video 1080p
  var ARROW_FILL = '#111111';
  var ARROW_STROKE = '#ffffff';
  var RIPPLE_SIZE = 40;                  // px, đường kính vòng gợn sóng khi nở hết cỡ
  var RIPPLE_MS = 450;
  var RIPPLE_COLOR = 'rgba(37, 99, 235, 0.9)';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  // Đầu mũi tên ở (0,0); thân đi xuống-phải như con trỏ chuẩn của hệ điều hành.
  var ARROW_PATH = 'M0,0 L0,19 L4.6,14.8 L8.2,22.6 L11.2,21.3 L7.7,13.6 L14,13.2 Z';

  function setup() {
    // document_start có thể chạy trước khi document.documentElement tồn tại (đặc biệt
    // trang XML/redirect sớm) — poll ngắn tới khi sẵn sàng thay vì gán trực tiếp.
    if (!document.documentElement) {
      setTimeout(setup, 10);
      return;
    }

    // Ẩn con trỏ OS gốc trong vùng trang (cho người xem Xvfb trực tiếp, vd qua VNC).
    // Bản quay KHÔNG dựa vào đây: recorder.py dùng ffmpeg -draw_mouse 0 vì con trỏ X
    // đứng yên và CSS không ẩn được nó ngoài vùng trang — REFERENCE.md §2/§4.
    var style = document.createElement('style');
    style.textContent = '* { cursor: none !important; }';

    var arrow = document.createElement('div');
    arrow.id = '__demo_cursor__';
    arrow.style.cssText = [
      'position: fixed',
      'top: 0', 'left: 0',
      'width: 0', 'height: 0',
      'pointer-events: none',   // không được chặn click thật của Playwright
      'z-index: 2147483647',    // luôn nổi trên mọi overlay/modal của app
      'transform-origin: 0 0',  // phóng to quanh ĐẦU mũi tên, không dời nó
      'will-change: transform',
      'display: none',
    ].join(';');

    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '23');
    svg.setAttribute('viewBox', '0 0 16 23');
    // !important + explicit size: the arrow sits in a 0x0 box and takes its size only from these, so a host page rule
    // such as `svg { max-width: 100%; height: auto }` must not be able to shrink it to nothing (measured: 0x0).
    [['width', '16px'], ['height', '23px'], ['min-width', '0'], ['min-height', '0'],
     ['max-width', 'none'], ['max-height', 'none'], ['display', 'block'], ['overflow', 'visible'],
     ['margin', '0'], ['padding', '0'], ['vertical-align', 'baseline'],
     ['filter', 'drop-shadow(0 1px 2px rgba(0,0,0,0.45))']
    ].forEach(function (rule) { svg.style.setProperty(rule[0], rule[1], 'important'); });
    var path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', ARROW_PATH);
    path.setAttribute('fill', ARROW_FILL);
    path.setAttribute('stroke', ARROW_STROKE);
    path.setAttribute('stroke-width', '1.2');
    path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
    arrow.appendChild(svg);

    function mount() {
      document.documentElement.appendChild(style);
      document.documentElement.appendChild(arrow);
    }
    if (document.body) mount();
    else document.addEventListener('DOMContentLoaded', mount);

    document.addEventListener('mousemove', function (e) {
      arrow.style.display = 'block';
      arrow.style.transform = 'translate(' + e.clientX + 'px, ' + e.clientY + 'px) scale(' + ARROW_SCALE + ')';
    }, { capture: true, passive: true });

    // Vòng gợn sóng tại điểm nhấn: nở ra và mờ dần rồi tự xoá. pointer-events none để không chặn click.
    document.addEventListener('mousedown', function (e) {
      var ring = document.createElement('div');
      ring.className = '__demo_ripple__';
      ring.style.cssText = [
        'position: fixed',
        'left: ' + e.clientX + 'px', 'top: ' + e.clientY + 'px',
        'width: ' + RIPPLE_SIZE + 'px', 'height: ' + RIPPLE_SIZE + 'px',
        'margin: ' + (-RIPPLE_SIZE / 2) + 'px 0 0 ' + (-RIPPLE_SIZE / 2) + 'px',
        'box-sizing: border-box',
        'border-radius: 50%',
        'border: 3px solid ' + RIPPLE_COLOR,
        'pointer-events: none',
        'z-index: 2147483646',   // dưới mũi tên
      ].join(';');
      document.documentElement.appendChild(ring);
      function remove() { if (ring.parentNode) ring.parentNode.removeChild(ring); }
      if (ring.animate) {
        var anim = ring.animate(
          [{ transform: 'scale(0.2)', opacity: 0.9 }, { transform: 'scale(1)', opacity: 0 }],
          { duration: RIPPLE_MS, easing: 'ease-out', fill: 'forwards' }
        );
        anim.onfinish = remove;
      }
      setTimeout(remove, RIPPLE_MS + 150);   // dự phòng: tab ẩn/animation bị chặn thì vẫn không để rác lại
    }, { capture: true, passive: true });
  }

  setup();
})();
