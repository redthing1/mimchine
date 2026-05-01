# mimchine

well-integrated **mini-machines**; a portable linux that can have host dirs linked in. inspired by [distrobox](https://github.com/89luca89/distrobox) and powered by podman.

## what it's about

sometimes, i want a linux terminal development environment on macos or linux, and i want it to feel like my machine. maybe i mount a whole dev tree, so that i can seamlessly do things.

build an image, enter a container, then get a shell.

## setup

### linux

should be all good to go

### macos

ensure podman machine is initialized as such:

```sh
podman machine init --volume /Users --volume /Volumes
podman machine stop && ulimit -n unlimited && podman machine start
```

## usage

sync project environment:

```sh
uv sync
```

build a mimchine image:

```sh
uv run mimchine build -f ./demo/mim_fed_demo.docker -n mim_fed_demo
```

create one with a workspace:

```sh
uv run mimchine create -n mim_fed_demo -c mim_fed_demo -W ~/Dev/project
```

mount anything else directly:

```sh
uv run mimchine create \
  -n mim_fed_demo \
  -M ~/.stuff:/home/user/.stuff:rw \
  -M ~/Downloads/reference:/refs:ro
```

if you repeat the same mounts a lot, put them in `~/.config/mimchine/config.toml`:

```toml
[profiles.work]
workspaces = ["~/Dev/project"]
mounts = ["~/.stuff:/home/user/.stuff:rw"]
network = "default"
```

then:

```sh
uv run mimchine enter -n mim_fed_demo -c mim_fed_demo -P work
```

other useful snippets:

```sh
uv run mimchine shell -c mim_fed_demo
uv run mimchine inspect -c mim_fed_demo
uv run mimchine export -n mim_fed_demo -o ~/Downloads/mim_fed_demo.tar.zst
uv run mimchine import -i ~/Downloads/mim_fed_demo.tar.zst
uv run mimchine destroy -c mim_fed_demo -f
```
