# Subir VODs de Kick a YouTube por partes

Este workflow procesa **un VOD por ejecución**, empezando por el más antiguo:

1. Consulta los VODs del canal de Kick.
2. Revisa en YouTube qué VODs o partes ya fueron subidos.
3. Abre el playlist HLS del VOD y elige la variante de mayor calidad.
4. Suma la duración real de los segmentos `EXTINF`.
5. Divide el VOD en partes consecutivas de duración similar.
6. Ninguna parte supera el límite configurado, por defecto 11 h 50 min.
7. Genera, sube y elimina una parte antes de procesar la siguiente.
8. Si una subida falla, la próxima ejecución continúa desde la parte faltante.

## División HLS

Los cortes se hacen **entre segmentos HLS**, no dentro de un segmento ni por
cantidad de segmentos. Así se conserva la calidad y no se recodifica.

Ejemplos con el límite predeterminado:

- 10 horas: 1 parte.
- 18 horas: 2 partes de aproximadamente 9 horas.
- 23 horas: 2 partes de aproximadamente 11 h 30 min.
- 25 horas: 3 partes de aproximadamente 8 h 20 min.

El script conserva las referencias `EXT-X-KEY`, `EXT-X-MAP`, `BYTERANGE` y
`DISCONTINUITY` asociadas a cada segmento al crear los playlists temporales.
También resuelve correctamente las URLs relativas mediante la URL real del
playlist.

## Formato de los videos

Sin división:

```text
Vector | DD/MM/AAAA
```

Con división:

```text
Vector | DD/MM/AAAA | 1/2
Vector | DD/MM/AAAA | 2/2
```

La descripción mantiene exactamente estas tres líneas:

```text
<session_title>
<start_time>
<source>
```

Cada parte se identifica con una etiqueta independiente:

```text
kick-vod-<ID>-part-1-of-2
kick-vod-<ID>-part-2-of-2
```

Esto permite continuar una carga incompleta sin duplicar las partes ya
publicadas. También se reconocen los identificadores antiguos
`kick-vod-id:<ID>`.

## Comando de FFmpeg

Cada playlist temporal se descarga y concatena sin recodificación:

```bash
ffmpeg \
  -protocol_whitelist file,http,https,tcp,tls,crypto,data \
  -i playlist_part.m3u8 \
  -c copy \
  part.ts
```

## Preparar la API de YouTube

1. Crea un proyecto en Google Cloud.
2. Habilita **YouTube Data API v3**.
3. Configura la pantalla de consentimiento OAuth.
4. Crea un cliente OAuth de tipo **Aplicación de escritorio**.
5. Descarga el archivo de credenciales, por ejemplo `client_secret.json`.

## Obtener el refresh token

```bash
python -m pip install -r requirements.txt
python get_youtube_token.py client_secret.json
```

Agrega los valores mostrados como secretos del repositorio:

- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

No subas `client_secret.json` al repositorio.

## Configuración del workflow

En `.github/workflows/upload-youtube.yml` puedes cambiar:

- `KICK_CHANNEL`: canal de Kick.
- `YOUTUBE_PRIVACY_STATUS`: `private`, `unlisted` o `public`.
- `YOUTUBE_CATEGORY_ID`: `20` corresponde a Gaming.
- `MAX_PART_DURATION_SECONDS`: duración máxima de cada parte. El valor
  predeterminado `42600` equivale a 11 horas y 50 minutos.

## Uso de disco

El script conserva solamente una parte `.ts` a la vez. Si una parte aún resulta
demasiado grande para el espacio disponible en el runner, reduce
`MAX_PART_DURATION_SECONDS`, por ejemplo a `14400` para partes de 4 horas.

## VOD manual desde Internet Archive

El workflow `.github/workflows/upload-youtube.yml` también permite subir una VOD
que ya fue archivada en Internet Archive aunque haya desaparecido de Kick.

Al ejecutar manualmente la acción **Upload oldest VOD to YouTube**, aparece el
campo **archive_url**. Pega una URL como:

```text
https://archive.org/details/vector-kick-vod-119757223
```

También se acepta una URL copiada desde la vista de código fuente:

```text
view-source:https://archive.org/details/vector-kick-vod-119757223
```

Cuando `archive_url` tiene un valor:

1. No se consulta la API de VODs de Kick.
2. Se obtiene la metadata pública del ítem de Internet Archive.
3. Se localiza automáticamente el MP4 principal.
4. Se descarga el archivo al workspace del runner.
5. Si supera `MAX_PART_DURATION_SECONDS`, se divide en partes `.ts` sin
   recodificar.
6. Las partes se suben a YouTube usando el mismo sistema de etiquetas y
   reanudación del flujo normal.
7. Los archivos temporales se eliminan al finalizar cada parte.

Cuando **archive_url** se deja vacío, el workflow mantiene exactamente el
comportamiento anterior y procesa el VOD pendiente más antiguo de Kick.

