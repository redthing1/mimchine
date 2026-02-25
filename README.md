
# mimchine

well-integrated **mini-machines**; a portable linux that has all your data dirs mounted. inspired by [distrobox](https://github.com/89luca89/distrobox) and powered by podman.

## what it's about

sometimes, i want a linux terminal development environment on macos, and i want all my data magically linked in. so that i can cd to a source directory and seamlessly build it.

with the power of containers, we can do just that. we run a linux userspace of our choice (fully customizable by a dockerfile), and mount in all our directories.

mimchine makes the above super easy. just build a machine image, create a container, then run `mimchine shell` and you're in!

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

### sync project environment

```sh
uv sync
```

### build a mimchine image

```sh
uv run mimchine build -f ./demo/mim_fed.docker -n mim_fed
```

### create a mimchine

```sh
uv run mimchine create -n mim_fed -H ~/Downloads
```

### open a shell in a mimchine

```sh
uv run mimchine shell -c mim_fed
```

### destroy a mimchine

```sh
uv run mimchine destroy -c mim_fed -f
```
