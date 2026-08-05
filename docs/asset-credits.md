# 素材与第三方库署名

小镇界面用到的所有美术素材和前端库都随仓库一起入库（`web/assets/`、`web/vendor/`），
不在运行时从 CDN 拉取。原因有两个：离线也能演示；以及部署镜像里不能出现「构建时能跑、
运行时挂掉」的外部依赖。

## 美术素材

| 素材 | 来源 | 许可 | 仓库位置 |
| --- | --- | --- | --- |
| Kenney Tiny Town 1.1（2023-01-11） | [Kenney](https://kenney.nl/assets/tiny-town) | CC0 1.0 Universal（公共领域） | `web/assets/kenney/tiny-town.png` |
| Kenney Tiny Dungeon 1.0（2022-07-05） | [Kenney](https://kenney.nl/assets/tiny-dungeon) | CC0 1.0 Universal（公共领域） | `web/assets/kenney/tiny-dungeon.png` |

两份素材的原始许可文本原样保留在 `web/assets/kenney/tiny-town-License.txt` 和
`web/assets/kenney/tiny-dungeon-License.txt`，没有改写。

CC0 表示作者放弃了全部著作权，个人、教育和商业用途都不需要授权，署名也不是强制的。
这里仍然写清来源：**美术素材由 [Kenney](https://kenney.nl) 制作。** 作者只要求「有余力
就署名或赞助」，这一行是履行那个请求，不是履行许可义务。

两张图都是 16×16 的瓦片表，12 列 × 11 行 = 132 格，瓦片索引 = 行 × 12 + 列。
`web/game/town-scene.js` 顶部的 `T` 表把用到的索引映射成语义名（屋顶、屋檐、门、窗、
围墙九宫格、道具），场景里不出现裸数字——否则换素材时没人知道 `109` 指的是哪块地板。

- `tiny-town.png` 提供地面、树木、道路、围墙九宫格、房屋屋顶与门窗、招牌和箱桶道具。
- `tiny-dungeon.png` 只用来取角色精灵：Tiny Town 整张表里只有 1 个人物（索引 104），
  7 个角色需要 7 张不同的脸。

## 前端库

| 库 | 版本 | 许可 | 仓库位置 |
| --- | --- | --- | --- |
| Phaser | 3.90.0 | MIT | `web/vendor/phaser.min.js` |

Phaser 以传统 `<script>` 标签加载（`web/index.html`），这样 `window.Phaser` 存在，
`app.js` 才能作为 ES module 直接用它。整个 `web/` 目录零构建：没有 npm、没有 Vite、
没有 TypeScript 编译步骤，FastAPI 直接 `StaticFiles` 挂载，启动命令只有
`uv run job-agent-api` 一条。

MIT 要求保留版权与许可声明——`phaser.min.js` 文件头部的许可注释块没有被删除，
构建流程也不存在（文件是从上游发行版原样复制的），所以不会有工具把它剥掉。

## 自己写的部分

小镇的地图布局、道路生成、广场围墙、角色站位、气泡与镜头逻辑都在
`web/game/town-scene.js` 里，是针对本项目的真实事件模型写的，没有移植别的 RPG
项目的地图或场景代码。素材是 CC0 的瓦片，摆法是本项目的。
