/* Agent 小镇的 Phaser 场景。
 *
 * 这里只负责画；所有状态都由 app.js 通过 applySnapshot() 推进来，场景自己不去
 * fetch 任何东西。这样「页面画的每一笔都能反查到一条真实 ActivityEvent」这条
 * 约束在前端也成立：场景拿不到快照就什么都不动，不会自己编一个动画出来。
 */

const TILE = 16;
const MAP_COLS = 44;
const MAP_ROWS = 28;
/** 一栋房子占几行（2 行屋顶 + 2 行墙）。门口地面在第 BUILDING_ROWS 行。 */
const BUILDING_ROWS = 4;

/* Tiny Town 的瓦片索引（16×16、12 列，索引即 row * 12 + col）。
 *
 * 索引是逐格看过带编号的 contact sheet 定下来的，几个容易记错的：43 是「草地上的
 * 踏石」而不是石板路（整片铺开会露草，看着像碎石）；74/78 是黑色门洞，真正的门是
 * 85–91；51/55 是屋顶上的天窗，正好给屋顶一张「脸」。
 */
const T = {
  grassPlain: 0,
  grassTuft: 1,
  grassFlower: 2,
  dirt: 40,
  dirtSpeck: 41,
  stepStone: 43,
  // 96–98 / 108–110 / 120–122 是一套完整的城墙 nine-slice，109 是纯地砖。
  wallTopL: 96, wallTopM: 97, wallTopR: 98,
  floorL: 108, floor: 109, floorR: 110,
  wallBotL: 120, wallBotM: 121, wallBotR: 122,
  gate: 103,
  roofGreyL: 48, roofGreyM: 49, roofGreyR: 50, roofGreyAttic: 51,
  roofRedL: 52, roofRedM: 53, roofRedR: 54, roofRedAttic: 55,
  eaveGreyL: 60, eaveGreyM: 61, eaveGreyR: 62,
  eaveRedL: 64, eaveRedM: 65, eaveRedR: 66,
  wallBrownL: 72, wallBrownR: 73, wallBrown: 75,
  wallGreyL: 76, wallGreyR: 77, wallGrey: 79,
  windowBrown: 84, doorBrown: 86, windowGrey: 88, doorGrey: 90,
  fenceH: 44, signPost: 83,
  treeTop: 4, treeBot: 16,
  bush: 5, mushroom: 29, plants: 17,
  crate: 130, barrel: 131, gold: 93, hive: 94, target: 95,
  pickaxe: 115, fork: 116, key: 117, bow: 118,
  shovel: 127, hammer: 128, scythe: 129,
};

/* 建筑↔角色的固定布局。
 *
 * 网格坐标（瓦片），不是百分比：Phaser 场景是一张真实的瓦片地图，建筑要落在整格
 * 上，否则墙缝会有半像素裂纹。x/y 是建筑左上角。
 */
const PLACES = {
  job_scout:        {x: 4,  y: 3,  roof: 'grey', wall: 'brown', sign: T.gold,    label: 'Scout Outpost'},
  job_analyst:      {x: 15, y: 2,  roof: 'red',  wall: 'grey',  sign: T.key,     label: 'JD Lab'},
  match_scorer:     {x: 33, y: 3,  roof: 'grey', wall: 'grey',  sign: T.pickaxe, label: 'Match Bureau'},
  material_builder: {x: 4,  y: 19, roof: 'red',  wall: 'brown', sign: T.hammer,  label: 'Material Workshop'},
  interview_coach:  {x: 29, y: 21, roof: 'red',  wall: 'grey',  sign: T.target,  label: 'Interview Arena'},
  judge:            {x: 37, y: 12, roof: 'grey', wall: 'grey',  sign: T.bow,     label: 'Evidence Court'},
};

// Plaza 是路由器所在的地方，所以 dispatch 线从这里出发。
const PLAZA = {x: 19, y: 11, w: 6, h: 5};

// 角色精灵取自 Tiny Dungeon 的人物那几行。单帧素材，没有 walk 序列——所以
// 「在动」靠上下小幅 bob 和朝向翻转表达，而不是假装有走路动画。
const SPRITES = {
  job_scout: 112,
  job_analyst: 84,
  match_scorer: 88,
  material_builder: 98,
  interview_coach: 96,
  judge: 97,
};
const FALLBACK_SPRITES = [99, 100, 86, 87, 109, 111, 110];

const ROUTE_COLORS = {
  dispatch: 0x7cf2bd,
  handoff: 0x82b9ff,
  review: 0xf5c873,
};

const STATUS_TINT = {
  running: 0xffffff,
  ok: 0xd6e9ff,
  completed: 0xd6e9ff,
  error: 0xffb0a8,
  timeout: 0xffb0a8,
  queued: 0xf2e6b8,
  idle: 0xbfcfc9,
  disabled: 0x6d7a76,
};

const short = (value, size) => {
  const text = String(value ?? '');
  return text.length > size ? `${text.slice(0, size - 1)}…` : text;
};

export class TownScene extends Phaser.Scene {
  constructor() {
    super({key: 'town'});
    /** 最近一次 applySnapshot 的数据，供 create() 之后的第一次绘制使用。 */
    this.pending = null;
    this.ready = false;
    this.buildings = new Map();
    this.actors = new Map();
    this.onSelect = () => {};
  }

  preload() {
    this.load.spritesheet('town', 'assets/kenney/tiny-town.png', {
      frameWidth: TILE, frameHeight: TILE,
    });
    this.load.spritesheet('folk', 'assets/kenney/tiny-dungeon.png', {
      frameWidth: TILE, frameHeight: TILE,
    });
  }

  create() {
    this.cameras.main.setBackgroundColor('#0b1713');
    this.ground = this.add.group();
    this.paintGround();
    this.paintPaths();
    this.paintPlaza();
    this.paintScenery();

    // 路线画在建筑下面、地面上面，这样线不会盖住门口的精灵。
    this.routeLayer = this.add.graphics().setDepth(2);
    this.routePulses = this.add.group();

    this.buildingLayer = this.add.container(0, 0).setDepth(3);
    this.actorLayer = this.add.container(0, 0).setDepth(6);

    this.setupCamera();

    this.ready = true;
    if (this.pending) this.applySnapshot(this.pending);
  }

  /* ---------- 地图 ---------- */

  /* 坐标哈希当伪随机，而不是 Math.random()：草丛位置必须是坐标的纯函数，否则
   * 每 1.5 秒一次的轮询重绘会让整片草地闪。
   *
   * 混一遍高位再取模：`(x * a) ^ (y * b)` 的低位周期性很强，直接 `% 100` 会让
   * 「6% 开花」实际长成一片花海。
   */
  static noise(x, y) {
    let h = (x * 374761393 + y * 668265263) | 0;
    h = (h ^ (h >>> 13)) * 1274126177;
    return ((h ^ (h >>> 16)) >>> 0) % 100;
  }

  paintGround() {
    for (let y = 0; y < MAP_ROWS; y += 1) {
      for (let x = 0; x < MAP_COLS; x += 1) {
        const roll = TownScene.noise(x, y);
        const frame = roll < 3 ? T.grassFlower : roll < 17 ? T.grassTuft : T.grassPlain;
        this.add.image(x * TILE, y * TILE, 'town', frame).setOrigin(0).setDepth(0);
      }
    }
  }

  /* 从每家门口铺一条土路到广场。
   *
   * 路本身不表示任何状态（状态是那些彩色路线），但没有路的话七栋房子看着像随机
   * 扔在草地上的贴图，不像一个镇子。
   */
  paintPaths() {
    const anchorY = PLAZA.y + Math.floor(PLAZA.h / 2);
    const anchorX = PLAZA.x + Math.floor(PLAZA.w / 2);
    const inPlaza = (x, y) => x >= PLAZA.x && x < PLAZA.x + PLAZA.w
      && y >= PLAZA.y && y < PLAZA.y + PLAZA.h;
    const tiles = new Set();
    for (const place of Object.values(PLACES)) {
      const door = this.doorTile(place);
      const stepY = door.y < anchorY ? 1 : -1;
      for (let y = door.y; y !== anchorY; y += stepY) tiles.add(`${door.x},${y}`);
      const stepX = door.x < anchorX ? 1 : -1;
      for (let x = door.x; x !== anchorX; x += stepX) tiles.add(`${x},${anchorY}`);
    }
    for (const key of tiles) {
      const [x, y] = key.split(',').map(Number);
      if (inPlaza(x, y)) continue;
      const frame = TownScene.noise(x, y) < 26 ? T.dirtSpeck : T.dirt;
      this.add.image(x * TILE, y * TILE, 'town', frame).setOrigin(0).setDepth(1);
    }
  }

  /* 广场用一整套九宫格城墙围出来的庭院。
   *
   * 96/97/98、108/109/110、120/121/122 是同一套 nine-slice：角、边、内。整片铺
   * 单张地砖会变成一块灰色泳池，围起来才像镇中心；主街撞上墙的那两格换成门
   * （103），不然街道看着直接怼在墙上。
   */
  paintPlaza() {
    const {x, y, w, h} = PLAZA;
    const gateY = y + Math.floor(h / 2);
    for (let dy = 0; dy < h; dy += 1) {
      for (let dx = 0; dx < w; dx += 1) {
        const left = dx === 0;
        const right = dx === w - 1;
        const top = dy === 0;
        const bottom = dy === h - 1;
        let frame = T.floor;
        if (top) frame = left ? T.wallTopL : right ? T.wallTopR : T.wallTopM;
        else if (bottom) frame = left ? T.wallBotL : right ? T.wallBotR : T.wallBotM;
        else if (left || right) frame = y + dy === gateY ? T.gate : (left ? T.floorL : T.floorR);
        this.add.image((x + dx) * TILE, (y + dy) * TILE, 'town', frame)
          .setOrigin(0).setDepth(1);
      }
    }
    // 中央的告示牌就是路由器：所有 dispatch 线都从这块牌子出发。
    this.add.image(
      (x + w / 2) * TILE, (y + h / 2) * TILE - 4, 'town', T.signPost,
    ).setOrigin(0.5).setDepth(2);
    // 院子里丢两件杂物，不然 4×3 的空地砖看着像一块没画完的底图。
    this.add.image((x + 1) * TILE + 2, (y + 1) * TILE + 3, 'town', T.crate)
      .setOrigin(0).setDepth(2).setScale(0.8);
    this.add.image((x + w - 2) * TILE + 2, (y + h - 2) * TILE - 1, 'town', T.barrel)
      .setOrigin(0).setDepth(2).setScale(0.8);
    this.add.text(
      (x + w / 2) * TILE, (y + h) * TILE + 2, 'LANGGRAPH PLAZA',
      {fontFamily: 'ui-monospace, monospace', fontSize: '7px', color: '#7cf2bd'},
    ).setOrigin(0.5, 0).setDepth(4);
  }

  paintScenery() {
    // 树和栅栏纯装饰，位置写死，不承载任何状态含义。
    const trees = [
      [1, 8], [2, 14], [11, 8], [26, 2], [41, 8], [42, 18],
      [1, 25], [12, 26], [24, 26], [35, 26], [40, 24], [9, 1],
    ];
    for (const [x, y] of trees) {
      this.add.image(x * TILE, (y - 1) * TILE, 'town', T.treeTop).setOrigin(0).setDepth(1);
      this.add.image(x * TILE, y * TILE, 'town', T.treeBot).setOrigin(0).setDepth(1);
    }
    for (const [x, y] of [[6, 10], [30, 9], [15, 17], [36, 20], [21, 25]]) {
      this.add.image(x * TILE, y * TILE, 'town', T.bush).setOrigin(0).setDepth(1);
    }
    for (const [x, y] of [[7, 16], [33, 17]]) {
      this.add.image(x * TILE, y * TILE, 'town', T.mushroom).setOrigin(0).setDepth(1);
    }
    for (const [x, y] of [[13, 6], [28, 15], [10, 22]]) {
      this.add.image(x * TILE, y * TILE, 'town', T.plants).setOrigin(0).setDepth(1);
    }
  }

  /* ---------- 建筑 ---------- */

  placeFor(roleId, index) {
    if (PLACES[roleId]) return PLACES[roleId];
    // 注册表可以随时加角色，所以没有预设坐标的角色沿地图下缘顺次落座，
    // 而不是全叠在 (0,0)。
    const slot = index % 6;
    return {
      x: 3 + slot * 7,
      y: slot % 2 ? 21 : 22,
      roof: slot % 2 ? 'red' : 'grey',
      wall: slot % 2 ? 'grey' : 'brown',
      sign: T.crate,
      label: '',
    };
  }

  /** 建筑门口的地面格，精灵就站在这里。 */
  doorTile(place) {
    return {x: place.x + 1, y: place.y + BUILDING_ROWS};
  }

  /* 一栋房子是四行瓦片：脊瓦（中间开天窗）→ 檐瓦 → 墙 → 带门窗的墙。
   *
   * 屋顶必须两行，48-50 是带屋脊的上坡、60-62 是带檐口的下坡，少一行就不成形；
   * 墙也要两行，只有一行的话屋顶压着门，看着像个屋顶直接落在地上的棚子。门用
   * 85–91 的实心门，74/78 是没装门的黑洞。
   */
  drawBuilding(place) {
    const red = place.roof === 'red';
    const grey = place.wall === 'grey';
    const rows = [
      red ? [T.roofRedL, T.roofRedAttic, T.roofRedR]
        : [T.roofGreyL, T.roofGreyAttic, T.roofGreyR],
      red ? [T.eaveRedL, T.eaveRedM, T.eaveRedR]
        : [T.eaveGreyL, T.eaveGreyM, T.eaveGreyR],
      grey ? [T.wallGreyL, T.wallGreyR, T.wallGrey]
        : [T.wallBrownL, T.wallBrownR, T.wallBrown],
      grey ? [T.windowGrey, T.doorGrey, T.wallGrey]
        : [T.windowBrown, T.doorBrown, T.wallBrown],
    ];
    const parts = [];
    rows.forEach((row, dy) => {
      row.forEach((frame, dx) => {
        parts.push(
          this.add.image((place.x + dx) * TILE, (place.y + dy) * TILE, 'town', frame)
            .setOrigin(0),
        );
      });
    });
    // 招牌挂在门右上方的墙上，让 7 栋建筑在同一套瓦片下也能一眼区分。
    parts.push(
      this.add.image((place.x + 2) * TILE + 4, (place.y + 2) * TILE + 4, 'town', place.sign)
        .setOrigin(0).setScale(0.58),
    );
    return parts;
  }

  ensureBuilding(role, index) {
    if (this.buildings.has(role.role_id)) return this.buildings.get(role.role_id);
    const place = this.placeFor(role.role_id, index);
    const label = role.town_place || place.label || role.display_name;
    const container = this.add.container(0, 0);
    for (const part of this.drawBuilding(place)) container.add(part);

    const nameplate = this.add.text(
      (place.x + 1.5) * TILE, (place.y - 1) * TILE + 4,
      `${role.town_icon || ''}${label}`.trim(),
      {
        // 建筑图标是 emoji，字体栈里必须带 emoji 字体：Canvas 里没有 HTML 那套
        // 隐式回退，缺了就画成豆腐块。
        fontFamily: '"PingFang SC", "Apple Color Emoji", "Segoe UI Emoji", system-ui, sans-serif',
        fontSize: '8px',
        color: '#e6f4ee',
        backgroundColor: '#0a1511cc',
        padding: {x: 3, y: 1},
      },
    ).setOrigin(0.5, 1);
    container.add(nameplate);

    // 整栋建筑可点，命中区就是那 3×3 格；只在建筑上开交互，避免整张地图变成按钮。
    const hit = this.add.rectangle(
      place.x * TILE, place.y * TILE, TILE * 3, TILE * BUILDING_ROWS, 0xffffff, 0,
    ).setOrigin(0).setInteractive({useHandCursor: true});
    hit.on('pointerup', () => this.onSelect(role.role_id));
    container.add(hit);

    const record = {place, container, nameplate, parts: container.list.slice(), glow: null};
    this.buildings.set(role.role_id, record);
    this.buildingLayer.add(container);
    return record;
  }

  /* ---------- 角色精灵 ---------- */

  ensureActor(role, index) {
    if (this.actors.has(role.role_id)) return this.actors.get(role.role_id);
    const place = this.placeFor(role.role_id, index);
    const door = this.doorTile(place);
    const frame = SPRITES[role.role_id]
      ?? FALLBACK_SPRITES[index % FALLBACK_SPRITES.length];

    const container = this.add.container(door.x * TILE + TILE / 2, door.y * TILE + TILE / 2);
    const sprite = this.add.image(0, 0, 'folk', frame).setOrigin(0.5);
    const nameplate = this.add.text(0, 11, role.display_name, {
      fontFamily: 'ui-monospace, monospace',
      fontSize: '7px',
      color: '#dce9e4',
      backgroundColor: '#040c09cc',
      padding: {x: 2, y: 1},
    }).setOrigin(0.5, 0);

    const bubbleBox = this.add.graphics();
    const bubbleText = this.add.text(0, 0, '', {
      fontFamily: '"PingFang SC", "Apple Color Emoji", "Segoe UI Emoji", system-ui, sans-serif',
      fontSize: '7px',
      color: '#d8e8e2',
      // useAdvancedWrap 必须开：Phaser 默认只在空格处断行，中文整段没有空格，
      // 结果气泡会横着长成一条，穿过隔壁房子。advanced 模式按字断。
      wordWrap: {width: 96, useAdvancedWrap: true},
      lineSpacing: 2,
    }).setOrigin(0, 0.5);
    const bubble = this.add.container(0, -6, [bubbleBox, bubbleText]).setVisible(false);

    container.add([bubble, sprite, nameplate]);
    this.actorLayer.add(container);

    const record = {
      container, sprite, nameplate, bubble, bubbleBox, bubbleText,
      home: {x: container.x, y: container.y},
      // 气泡朝地图内侧横着展开：顶在头上会盖住自家屋顶，朝地图外会被镜头切掉。
      side: place.x + 1 < MAP_COLS / 2 ? 1 : -1,
      bob: null,
      speech: '',
    };
    this.actors.set(role.role_id, record);
    return record;
  }

  setBubble(actor, text) {
    if (actor.speech === text) return;
    actor.speech = text;
    if (!text) {
      actor.bubble.setVisible(false);
      return;
    }
    actor.bubbleText.setText(text);
    const w = Math.max(actor.bubbleText.width, 30) + 8;
    const h = actor.bubbleText.height + 8;
    // 34px ≈ 房子中线到外墙的距离：从这里起画，气泡整块落在房子外面。
    const gap = 34 * actor.side;
    const left = actor.side > 0 ? gap : gap - w;
    actor.bubbleText.setPosition(left + 4, 0);
    actor.bubbleBox.clear();
    actor.bubbleBox.fillStyle(0x08140f, 0.94);
    actor.bubbleBox.lineStyle(1, 0x47685d, 1);
    actor.bubbleBox.fillRoundedRect(left, -h / 2, w, h, 3);
    actor.bubbleBox.strokeRoundedRect(left, -h / 2, w, h, 3);
    // 小尾巴指回精灵，否则气泡看着像飘在空中和谁都没关系。
    const tip = actor.side > 0 ? left : left + w;
    actor.bubbleBox.fillTriangle(tip, -3, tip, 3, tip - 5 * actor.side, 1);
    actor.bubble.setVisible(true);
  }

  setBob(actor, on) {
    if (on && !actor.bob) {
      actor.bob = this.tweens.add({
        targets: actor.sprite,
        y: -3,
        duration: 330,
        yoyo: true,
        repeat: -1,
        ease: 'Sine.easeInOut',
      });
    } else if (!on && actor.bob) {
      actor.bob.stop();
      actor.bob = null;
      actor.sprite.y = 0;
    }
  }

  /* ---------- 路线 ---------- */

  nodeCenter(roleId) {
    if (roleId === 'plaza') {
      return {
        x: (PLAZA.x + PLAZA.w / 2) * TILE,
        y: (PLAZA.y + PLAZA.h / 2) * TILE,
      };
    }
    const record = this.buildings.get(roleId);
    if (!record) return null;
    const {place} = record;
    return {x: (place.x + 1.5) * TILE, y: (place.y + 2) * TILE};
  }

  drawRoutes(routes) {
    this.routeLayer.clear();
    this.routePulses.clear(true, true);
    if (!routes?.length) return;
    const newest = routes.at(-1)?.timestamp;

    for (const route of routes) {
      const from = this.nodeCenter(route.source_role_id);
      const to = this.nodeCenter(route.target_role_id);
      if (!from || !to) continue;
      // 两端各内缩一点，线就不会从屋顶正中穿出来。
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const length = Math.hypot(dx, dy) || 1;
      const inset = Math.min(26, length / 3);
      const x1 = from.x + (dx / length) * inset;
      const y1 = from.y + (dy / length) * inset;
      const x2 = to.x - (dx / length) * inset;
      const y2 = to.y - (dy / length) * inset;

      const color = ROUTE_COLORS[route.kind] || 0xffffff;
      const live = route.timestamp === newest;
      this.routeLayer.lineStyle(live ? 2 : 1, color, live ? 0.95 : 0.4);
      this.routeLayer.beginPath();
      this.routeLayer.moveTo(x1, y1);
      this.routeLayer.lineTo(x2, y2);
      this.routeLayer.strokePath();

      if (!live) continue;
      // 最新那条线上跑一个光点，方向就看得出来——静态线只能表示两端相连。
      const pulse = this.add.circle(x1, y1, 2.2, color, 1).setDepth(5);
      this.routePulses.add(pulse);
      this.tweens.add({
        targets: pulse,
        x: x2, y: y2,
        duration: 1100,
        repeat: -1,
        ease: 'Sine.easeInOut',
      });
    }
  }

  /* ---------- 摄像机 ---------- */

  setupCamera() {
    const camera = this.cameras.main;
    // 边界比地图大很多圈：Phaser 的 clampX 会按 bounds 夹住 scroll，边界只比地图
    // 大一点的话 fitCamera 想把地图整体左移让开聊天窗的那段偏移会被吃掉。
    const pad = 420;
    camera.setBounds(-pad, -pad, MAP_COLS * TILE + pad * 2, MAP_ROWS * TILE + pad * 2);
    this.fitCamera();

    this.input.on('pointermove', pointer => {
      if (!pointer.isDown || pointer.event.target !== this.game.canvas) return;
      camera.scrollX -= (pointer.x - pointer.prevPosition.x) / camera.zoom;
      camera.scrollY -= (pointer.y - pointer.prevPosition.y) / camera.zoom;
    });
    this.input.on('wheel', (_pointer, _over, _dx, dy) => {
      const next = Phaser.Math.Clamp(camera.zoom - dy * 0.0016, this.minZoom * 0.75, 6);
      camera.setZoom(next);
    });
    this.scale.on('resize', () => this.fitCamera());
  }

  /* 默认缩放让整张地图刚好放得下，而不是开局就贴在某个角落。
   *
   * 右下角浮着聊天窗、底部还压着一条详情栏，所以可视区要按扣掉这两块之后来算，
   * 再把画面整体挪开——不然最右边的 Knowledge Library 和 Interview Arena 永远藏在聊天窗后面。
   */
  fitCamera() {
    const camera = this.cameras.main;
    const width = this.scale.width;
    const height = this.scale.height;
    const reserveX = width > 1200 ? 390 : 0;
    const reserveY = 96;
    const zoom = Math.min(
      Math.max(width - reserveX, 240) / (MAP_COLS * TILE),
      Math.max(height - reserveY, 200) / (MAP_ROWS * TILE),
    );
    this.minZoom = Math.max(zoom, 0.4);
    camera.setZoom(this.minZoom);
    camera.centerOn(
      (MAP_COLS * TILE) / 2 + reserveX / 2 / this.minZoom,
      (MAP_ROWS * TILE) / 2 + reserveY / 2 / this.minZoom,
    );
  }

  /* ---------- 对外接口 ---------- */

  /**
   * @param {object} snapshot
   * @param {Array} snapshot.roles      /api/roles 的角色列表（决定有哪些建筑）
   * @param {Array} snapshot.agents     每个角色的 {status, speech}
   * @param {Array} snapshot.routes     TownRoute[]
   * @param {string} snapshot.focusRoleId 当前选中的角色
   */
  applySnapshot(snapshot) {
    if (!this.ready) {
      this.pending = snapshot;
      return;
    }
    const {roles = [], agents = [], routes = [], focusRoleId = ''} = snapshot;
    const byId = new Map(agents.map(agent => [agent.role_id, agent]));

    roles.forEach((role, index) => {
      const building = this.ensureBuilding(role, index);
      const actor = this.ensureActor(role, index);
      const agent = byId.get(role.role_id) || {status: 'idle', speech: ''};
      const status = agent.status || 'idle';
      const selected = role.role_id === focusRoleId;

      actor.sprite.setTint(STATUS_TINT[status] || 0xffffff);
      actor.container.setAlpha(status === 'disabled' ? 0.4 : 1);
      this.setBob(actor, status === 'running');

      // 气泡只在工作中或被选中时出现。七个气泡常亮的话地图就没法看了。
      this.setBubble(
        actor,
        // 48 字 ≈ 气泡 4 行：全文在底部 HUD 和聊天窗里，这里只要够认出在干什么。
        status === 'running' || selected ? short(agent.speech, 48) : '',
      );
      // 被选中的建筑往上抬一点，配合侧栏详情，点了有反馈。
      building.container.setY(selected ? -3 : 0);
      building.nameplate.setColor(selected ? '#7cf2bd' : '#e6f4ee');

      // 排队中的角色走到 Plaza 等着——「等待调度」这件事得看得见。
      const queued = status === 'queued';
      const target = queued
        ? {
          x: (PLAZA.x + 1 + (index % 4) * 1.3) * TILE,
          y: (PLAZA.y + 1 + Math.floor(index / 4) * 1.6) * TILE,
        }
        : actor.home;
      // 记住目标点再比对：只看当前坐标的话，1.5 秒一次的轮询会在补间还没走完时
      // 又叠一个补间上去，越叠越多。
      const moving = actor.target
        && Math.abs(actor.target.x - target.x) < 1
        && Math.abs(actor.target.y - target.y) < 1;
      if (!moving) {
        actor.target = target;
        actor.sprite.setFlipX(target.x < actor.container.x);
        this.tweens.add({
          targets: actor.container,
          x: target.x, y: target.y,
          duration: 900,
          ease: 'Sine.easeInOut',
        });
      }
    });

    this.drawRoutes(routes);
  }

  focusOn(roleId) {
    const center = this.nodeCenter(roleId);
    if (!center) return;
    this.cameras.main.pan(center.x, center.y, 420, 'Sine.easeInOut');
  }
}

export function mountTown(parent, onSelect) {
  const scene = new TownScene();
  scene.onSelect = onSelect;
  const game = new Phaser.Game({
    type: Phaser.AUTO,
    parent,
    backgroundColor: '#0b1713',
    // 瓦片素材是像素画，插值会把它糊成一团。
    pixelArt: true,
    roundPixels: true,
    scale: {mode: Phaser.Scale.RESIZE, width: '100%', height: '100%'},
    scene,
  });
  return {game, scene};
}
